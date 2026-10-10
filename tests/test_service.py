"""The service's Unix socket and HTTP endpoint, without Bluetooth.

The service is built from a config with example meters but never started, so
no connection is attempted. HTTP tests talk to the real handler on an
ephemeral port on 127.0.0.1; socket tests use a temporary socket file.
"""
import asyncio
import errno
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from twiddle import config as configmod  # noqa: E402
from twiddle.service import Service  # noqa: E402


def make_config(**service) -> configmod.Config:
    cfg = configmod.Config()
    cfg.devices = {
        "meter-a": configmod.Device("meter-a", "AA:BB:CC:DD:EE:01", "USB load test"),
        "meter-b": configmod.Device("meter-b", "AA:BB:CC:DD:EE:02", "DC supply"),
    }
    for k, v in service.items():
        setattr(cfg, k, v)
    return cfg


def make_service(**service) -> Service:
    svc = Service(make_config(**service))
    for m in svc.managers.values():
        m.resets = []

        async def reset(counter="all", m=m):
            m.resets.append(counter)

        m.reset = reset
    return svc


async def _http(svc: Service, raw: bytes):
    server = await asyncio.start_server(svc._on_http, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(raw)
        await writer.drain()
        data = await reader.read()
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers, json.loads(body) if body else None


def http(svc, method="GET", path="/", raw=None, host="127.0.0.1:8731"):
    if raw is None:
        host_line = f"Host: {host}\r\n" if host is not None else ""
        raw = f"{method} {path} HTTP/1.1\r\n{host_line}Accept: */*\r\n\r\n".encode()
    return asyncio.run(_http(svc, raw))


class TestHttpStatus(unittest.TestCase):
    def test_list(self):
        status, headers, body = http(make_service(), path="/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertTrue(body["ok"])
        self.assertEqual(sorted(body["devices"]), ["meter-a", "meter-b"])

    def test_devices_alias(self):
        self.assertEqual(http(make_service(), path="/devices")[0], 200)

    def test_one_device(self):
        status, _, body = http(make_service(), path="/device/meter-a")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"]["mac"], "AA:BB:CC:DD:EE:01")

    def test_unknown_device_is_404(self):
        status, _, body = http(make_service(), path="/device/meter-z")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_unknown_path_is_404(self):
        status, _, body = http(make_service(), path="/reset")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_other_methods_are_405(self):
        for method in ("POST", "PUT", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                status, headers, body = http(make_service(), method=method,
                                             path="/device/meter-a")
                self.assertEqual(status, 405)
                self.assertEqual(headers.get("allow"), "GET")
                self.assertFalse(body["ok"])

    def test_malformed_request_is_400(self):
        status, _, body = http(make_service(), raw=b"HELLO\r\n\r\n")
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])


class TestHttpCors(unittest.TestCase):
    def test_no_cors_header_by_default(self):
        for path in ("/", "/device/meter-a", "/device/meter-z"):
            with self.subTest(path=path):
                _, headers, _ = http(make_service(), path=path)
                self.assertNotIn("access-control-allow-origin", headers)

    def test_configured_origin_only(self):
        svc = make_service(http_cors_origin="http://localhost:3000")
        _, headers, _ = http(svc, path="/")
        self.assertEqual(headers["access-control-allow-origin"], "http://localhost:3000")


class TestHttpHost(unittest.TestCase):
    """A page on another site can point its own host name at 127.0.0.1 (DNS
    rebinding) and read the answer as same-origin. Its Host header still
    names that site, so only local host names are answered."""

    def test_local_names_are_answered(self):
        for host in ("127.0.0.1:8731", "localhost:8731", "LOCALHOST", "127.0.0.1", None):
            with self.subTest(host=host):
                self.assertEqual(http(make_service(), path="/", host=host)[0], 200)

    def test_other_names_are_refused(self):
        for host in ("attacker.example.com:8731", "example.com", "127.0.0.1.example.com",
                     "localhost.example.com:8731"):
            with self.subTest(host=host):
                status, _, body = http(make_service(), path="/device/meter-a", host=host)
                self.assertEqual(status, 403)
                self.assertFalse(body["ok"])
                self.assertNotIn("status", body)


class TestSocketReset(unittest.TestCase):
    def test_reset_without_confirm_is_refused(self):
        svc = make_service()
        resp = asyncio.run(svc.handle({"cmd": "reset", "device": "meter-a", "counter": "all"}))
        self.assertFalse(resp["ok"])
        self.assertIn('"confirm": true', resp["error"])
        self.assertEqual(svc.managers["meter-a"].resets, [])

    def test_confirm_must_be_true_itself(self):
        svc = make_service()
        for confirm in (False, "true", "yes", 1, None):
            with self.subTest(confirm=confirm):
                resp = asyncio.run(svc.handle({"cmd": "reset", "device": "meter-a",
                                               "counter": "all", "confirm": confirm}))
                self.assertFalse(resp["ok"])
        self.assertEqual(svc.managers["meter-a"].resets, [])

    def test_reset_with_confirm(self):
        svc = make_service()
        resp = asyncio.run(svc.handle({"cmd": "reset", "device": "meter-a",
                                       "counter": "wh", "confirm": True}))
        self.assertEqual(resp, {"ok": True})
        self.assertEqual(svc.managers["meter-a"].resets, ["wh"])

    def test_unknown_device(self):
        resp = asyncio.run(make_service().handle({"cmd": "reset", "device": "meter-z",
                                                  "confirm": True}))
        self.assertFalse(resp["ok"])

    def test_over_the_socket(self):
        svc = make_service()

        async def go():
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "twiddle.sock")
                server = await asyncio.start_unix_server(svc._on_conn, path=path)
                try:
                    out = []
                    for req in ({"cmd": "reset", "device": "meter-b"},
                                {"cmd": "reset", "device": "meter-b", "confirm": True}):
                        reader, writer = await asyncio.open_unix_connection(path)
                        writer.write((json.dumps(req) + "\n").encode())
                        await writer.drain()
                        out.append(json.loads(await reader.readline()))
                        writer.close()
                    return out
                finally:
                    server.close()
                    await server.wait_closed()

        refused, done = asyncio.run(go())
        self.assertFalse(refused["ok"])
        self.assertTrue(done["ok"])
        self.assertEqual(svc.managers["meter-b"].resets, ["all"])


class TestBusyHttpPort(unittest.TestCase):
    """With the HTTP port taken, the service stops at once with one line,
    before it creates its socket or starts any meter connection."""

    def test_busy_port(self):
        from twiddle import cli, client

        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen()
        self.addCleanup(held.close)
        port = held.getsockname()[1]

        with tempfile.TemporaryDirectory() as d:
            sock = os.path.join(d, "twiddle.sock")
            cfg = os.path.join(d, "twiddle.toml")
            with open(cfg, "w") as fh:
                fh.write(f'[service]\nsocket = "{sock}"\nhttp_port = {port}\n\n'
                         f'[devices.meter-a]\nmac = "AA:BB:CC:DD:EE:01"\n')
            started = []

            async def no_meter(self):
                started.append(self.name)

            err = io.StringIO()
            with mock.patch.object(client.DeviceManager, "run", no_meter), \
                    mock.patch("twiddle.service.logging.basicConfig"), \
                    redirect_stderr(err):
                rc = cli.main(["--config", cfg, "service"])

            self.assertEqual(rc, 1)
            self.assertEqual(err.getvalue(), f"error: cannot listen on 127.0.0.1:{port}: "
                                             f"{os.strerror(errno.EADDRINUSE)}\n")
            self.assertFalse(os.path.exists(sock))
            self.assertEqual(started, [])


class TestConfigCors(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write(text)
        try:
            return configmod.load(fh.name)
        finally:
            os.unlink(fh.name)

    def test_default_is_off(self):
        self.assertIsNone(self.load("[service]\n").http_cors_origin)

    def test_loads_origin(self):
        cfg = self.load('[service]\nhttp_cors_origin = "http://localhost:3000"\n')
        self.assertEqual(cfg.http_cors_origin, "http://localhost:3000")

    def test_rejects_more_than_one_origin(self):
        for bad in ('"http://a.example.com http://b.example.com"',
                    '"http://a.example.com\\r\\nX-Evil: 1"', "3000", '""'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.load(f"[service]\nhttp_cors_origin = {bad}\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
