"""twiddle service: holds persistent BLE connections to every configured
tester and answers requests over a Unix-domain socket (newline-delimited
JSON). Optionally also serves a tiny read-only HTTP/JSON endpoint on
127.0.0.1.

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
  -> {"cmd": "reset", "device": "<name>", "counter": "all", "confirm": true}
  <- {"ok": true}
On error: {"ok": false, "error": "..."}.

A reset without "confirm": true is refused. That guards against a request
sent by mistake, not against a hostile one: the socket is created mode 0660,
so its owner and group decide who may read meters and reset counters.

HTTP (GET only): / and /devices list every meter, /device/<name> gives one.
Errors get 400, 403, 404 or 405. A Host header other than 127.0.0.1 or
localhost gets 403. No CORS header is sent unless [service] http_cors_origin
names one origin.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys

from . import __version__
from .client import DeviceManager
from .config import Config

log = logging.getLogger("twiddle.service")

HTTP_REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden",
                404: "Not Found", 405: "Method Not Allowed"}
# The HTTP server listens on 127.0.0.1 only. A browser always sends the Host
# it thinks it is talking to, so refusing any other name stops a web page
# that points its own host name at 127.0.0.1 (DNS rebinding) from reading it.
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def _host_name(host: str) -> str:
    """'localhost:8731' -> 'localhost'; '[::1]:8731' -> '[::1]'."""
    host = host.strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.split(":", 1)[0]


class StartError(Exception):
    """The service cannot start; the message says why."""


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
            if req.get("confirm") is not True:
                return {"ok": False, "error": 'reset clears the meter\'s counters and '
                        'needs "confirm": true in the request'}
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
        # Optional HTTP first: a port that is taken stops the start before the
        # socket is touched or any meter connection begins.
        http_server = None
        if self.cfg.http_port:
            try:
                http_server = await asyncio.start_server(
                    self._on_http, "127.0.0.1", self.cfg.http_port)
            except OSError as e:
                reason = os.strerror(e.errno) if e.errno else str(e)
                raise StartError(f"cannot listen on 127.0.0.1:{self.cfg.http_port}: "
                                 f"{reason}") from None

        sock = self.cfg.socket
        server = None
        try:
            # unix socket
            os.makedirs(os.path.dirname(sock) or ".", exist_ok=True)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(sock)
            server = await asyncio.start_unix_server(self._on_conn, path=sock)
            os.chmod(sock, 0o660)
            log.info("listening on %s (devices: %s)", sock, ", ".join(self.managers) or "none")
            if http_server:
                log.info("http json on http://127.0.0.1:%d/", self.cfg.http_port)

            # device supervisors
            for m in self.managers.values():
                self._tasks.append(asyncio.create_task(m.run(), name=f"dev:{m.name}"))

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for s in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(s, stop.set)
            await stop.wait()
        finally:
            log.info("shutting down")
            for m in self.managers.values():
                m.stop()
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            if http_server:
                http_server.close()
            if server:
                server.close()
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(sock)

    # -- minimal read-only HTTP (stdlib only) ----------------------------
    def http_route(self, method: str, path: str) -> tuple[int, dict]:
        """Map one request to (status, JSON body). Read-only: GET only."""
        if method != "GET":
            return 405, {"ok": False, "error": "read-only endpoint; use GET"}
        if path.rstrip("/") in ("", "/devices"):
            return 200, {"ok": True,
                         "devices": {n: m.status() for n, m in self.managers.items()}}
        if path.startswith("/device/"):
            name = path[len("/device/"):].strip("/")
            m = self.managers.get(name)
            if m:
                return 200, {"ok": True, "status": m.status()}
            return 404, {"ok": False, "error": f"unknown device {name!r}"}
        return 404, {"ok": False, "error": "not found; try / or /device/<name>"}

    async def _on_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request:
                return
            parts = request.decode("latin1").split()
            host = None
            while True:  # read the headers; only Host matters
                h = await asyncio.wait_for(reader.readline(), timeout=10)
                if h in (b"\r\n", b"\n", b""):
                    break
                name, _, value = h.decode("latin1").partition(":")
                if name.strip().lower() == "host":
                    host = value
            if not (len(parts) == 3 and parts[2].startswith("HTTP/")):
                status, body = 400, {"ok": False, "error": "bad request"}
            elif host is not None and _host_name(host) not in LOCAL_HOSTS:
                status, body = 403, {"ok": False,
                                     "error": "Host must be 127.0.0.1 or localhost"}
            else:
                status, body = self.http_route(parts[0], parts[1])
            payload = json.dumps(body).encode()
            head = [f"HTTP/1.1 {status} {HTTP_REASONS[status]}",
                    "Content-Type: application/json",
                    f"Content-Length: {len(payload)}",
                    "Connection: close"]
            if status == 405:
                head.append("Allow: GET")
            if self.cfg.http_cors_origin:
                # one origin, set by the operator; never "*" by default
                head.append(f"Access-Control-Allow-Origin: {self.cfg.http_cors_origin}")
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin1") + payload)
            await writer.drain()
        except Exception as e:
            log.debug("http error: %s", e)
        finally:
            with contextlib.suppress(Exception):
                writer.close()


def run(cfg: Config) -> int:
    """Run the service until SIGINT or SIGTERM. Returns the exit code."""
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(Service(cfg).run())
    except StartError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
