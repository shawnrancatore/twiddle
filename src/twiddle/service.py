"""twiddle service: holds persistent BLE connections to every configured
tester and answers requests over a Unix-domain socket (newline-delimited
JSON). Optionally also serves a tiny read-only HTTP/JSON endpoint.

Why a service? The meters allow only ONE BLE central at a time. The service
owns the single connection per device; the CLI and any agents talk to the
service instead of fighting over the radio.

IPC protocol (Unix socket, one JSON object per line, one response per line):
  -> {"cmd": "ping"}
  <- {"ok": true, "service": "twiddle", "version": "...", "devices": [...]}
  -> {"cmd": "list"}
  <- {"ok": true, "devices": {name: <status>, ...}}
  -> {"cmd": "get", "device": "<name>"}
  <- {"ok": true, "status": <status>}
  -> {"cmd": "reset", "device": "<name>", "counter": "all"}
  <- {"ok": true}
On error: {"ok": false, "error": "..."}.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal

from . import __version__
from .client import DeviceManager
from .config import Config

log = logging.getLogger("twiddle.service")


class Service:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        connect_lock = asyncio.Lock()  # shared: one scan+connect at a time
        self.managers: dict[str, DeviceManager] = {
            d.name: DeviceManager(d.name, d.mac, cfg.ble, d.note,
                                  connect_lock=connect_lock)
            for d in cfg.devices.values()
        }
        self._tasks: list[asyncio.Task] = []

    # -- request dispatch -------------------------------------------------
    async def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "ping":
            return {"ok": True, "service": "twiddle", "version": __version__,
                    "devices": list(self.managers)}
        if cmd == "list":
            return {"ok": True, "devices": {n: m.status() for n, m in self.managers.items()}}
        if cmd == "get":
            m = self.managers.get(req.get("device"))
            if not m:
                return {"ok": False, "error": f"unknown device {req.get('device')!r}"}
            return {"ok": True, "status": m.status()}
        if cmd == "reset":
            m = self.managers.get(req.get("device"))
            if not m:
                return {"ok": False, "error": f"unknown device {req.get('device')!r}"}
            try:
                await m.reset(req.get("counter", "all"))
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": False, "error": f"unknown cmd {cmd!r}"}

    # -- unix socket server ----------------------------------------------
    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=15)
            if not line:
                return
            try:
                req = json.loads(line)
            except json.JSONDecodeError as e:
                resp = {"ok": False, "error": f"bad json: {e}"}
            else:
                resp = await self.handle(req)
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        except Exception as e:
            log.debug("conn error: %s", e)
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def run(self) -> None:
        # device supervisors
        for m in self.managers.values():
            self._tasks.append(asyncio.create_task(m.run(), name=f"dev:{m.name}"))

        # unix socket
        sock = self.cfg.socket
        os.makedirs(os.path.dirname(sock) or ".", exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(sock)
        server = await asyncio.start_unix_server(self._on_conn, path=sock)
        os.chmod(sock, 0o660)
        log.info("listening on %s (devices: %s)", sock, ", ".join(self.managers) or "none")

        # optional HTTP
        http_server = None
        if self.cfg.http_port:
            http_server = await asyncio.start_server(
                self._on_http, "127.0.0.1", self.cfg.http_port)
            log.info("http json on http://127.0.0.1:%d/", self.cfg.http_port)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(s, stop.set)
        try:
            await stop.wait()
        finally:
            log.info("shutting down")
            for m in self.managers.values():
                m.stop()
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            server.close()
            if http_server:
                http_server.close()
            with contextlib.suppress(FileNotFoundError):
                os.unlink(sock)

    # -- minimal read-only HTTP (stdlib only) ----------------------------
    async def _on_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request:
                return
            parts = request.decode("latin1").split()
            path = parts[1] if len(parts) > 1 else "/"
            # drain headers
            while True:
                h = await asyncio.wait_for(reader.readline(), timeout=10)
                if h in (b"\r\n", b"\n", b""):
                    break
            if path.rstrip("/") in ("", "/devices"):
                body = {"ok": True, "devices": {n: m.status() for n, m in self.managers.items()}}
            elif path.startswith("/device/"):
                name = path[len("/device/"):].strip("/")
                m = self.managers.get(name)
                body = ({"ok": True, "status": m.status()} if m
                        else {"ok": False, "error": f"unknown device {name!r}"})
            else:
                body = {"ok": False, "error": "not found; try / or /device/<name>"}
            payload = json.dumps(body).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Access-Control-Allow-Origin: *\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(payload))
            writer.write(payload)
            await writer.drain()
        except Exception as e:
            log.debug("http error: %s", e)
        finally:
            with contextlib.suppress(Exception):
                writer.close()


def run(cfg: Config) -> None:
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(Service(cfg).run())
