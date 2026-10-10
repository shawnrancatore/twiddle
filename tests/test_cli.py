"""The CLI's side of a reset, and what it does when it may not use the
socket. The service is replaced by a captured request; nothing connects to a
meter."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from twiddle import cli  # noqa: E402
from twiddle import client  # noqa: E402

CONFIG = """
[service]
socket = "{socket}"

[devices.meter-a]
mac  = "AA:BB:CC:DD:EE:01"
note = "USB load test"
"""


class CliCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.socket = os.path.join(self.dir.name, "twiddle.sock")
        self.config = os.path.join(self.dir.name, "twiddle.toml")
        with open(self.config, "w") as fh:
            fh.write(CONFIG.format(socket=self.socket))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--config", self.config, *argv])
        return rc, out.getvalue(), err.getvalue()


def no_direct_connection(*_a, **_k):
    raise AssertionError("fell back to a direct Bluetooth connection")


class TestResetConfirm(CliCase):
    def test_yes_flag_sends_confirm(self):
        sent = []
        with mock.patch.object(cli, "_ipc", lambda _cfg, req: sent.append(req) or {"ok": True}):
            rc, out, _ = self.run_cli("reset", "meter-a", "--counter", "wh", "-y")
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [{"cmd": "reset", "device": "meter-a",
                                 "counter": "wh", "confirm": True}])

    def test_prompt_yes_sends_confirm(self):
        sent = []
        with mock.patch.object(cli, "_ipc", lambda _cfg, req: sent.append(req) or {"ok": True}), \
                mock.patch("builtins.input", return_value="y"):
            rc, _, _ = self.run_cli("reset", "meter-a")
        self.assertEqual(rc, 0)
        self.assertIs(sent[0]["confirm"], True)

    def test_prompt_no_sends_nothing(self):
        sent = []
        with mock.patch.object(cli, "_ipc", lambda _cfg, req: sent.append(req) or {"ok": True}), \
                mock.patch("builtins.input", return_value="n"):
            rc, out, _ = self.run_cli("reset", "meter-a")
        self.assertEqual(rc, 1)
        self.assertEqual(sent, [])
        self.assertIn("aborted", out)


class TestSocketPermission(CliCase):
    """A socket the user may not open means a service is there; the CLI must
    say so, not fall back to a direct connection that fights it."""

    def deny(self):
        real = cli.socket.socket

        class Denied(real):
            def connect(self, _addr):
                raise PermissionError(13, "Permission denied")

        return mock.patch.object(cli.socket, "socket", Denied)

    def test_get_does_not_fall_back(self):
        with self.deny(), mock.patch.object(client, "read_once", no_direct_connection):
            rc, _, err = self.run_cli("get", "meter-a")
        self.assertEqual(rc, 1)
        self.assertIn("permission denied", err)
        self.assertIn(self.socket, err)

    def test_reset_does_not_fall_back(self):
        with self.deny(), mock.patch.object(client, "reset_once", no_direct_connection):
            rc, _, err = self.run_cli("reset", "meter-a", "-y")
        self.assertEqual(rc, 1)
        self.assertIn("permission denied", err)

    def test_list(self):
        with self.deny():
            rc, _, err = self.run_cli("list")
        self.assertEqual(rc, 1)
        self.assertIn("permission denied", err)


class TestBadConfig(CliCase):
    def test_bad_cors_origin_is_a_usage_error(self):
        for bad in ("a b", "*", "null"):
            with self.subTest(bad=bad):
                with open(self.config, "w") as fh:
                    fh.write(f'[service]\nhttp_cors_origin = "{bad}"\n')
                rc, _, err = self.run_cli("config")
                self.assertEqual(rc, 2)
                self.assertIn("http_cors_origin", err)

    def test_malformed_config_is_one_line_not_a_traceback(self):
        cases = {
            "missing mac": ("[devices.meter-a]\nnote = 'x'\n", "devices.meter-a: mac is missing"),
            "device not a table": ("[devices]\nmeter-a = 'AA:BB:CC:DD:EE:01'\n",
                                   "devices.meter-a: must be a table"),
            "http_port wrong type": ("[service]\nhttp_port = 'abc'\n", "http_port"),
            "mac wrong type": ("[devices.meter-a]\nmac = 5\n", "devices.meter-a: mac must be text"),
        }
        for label, (text, expect) in cases.items():
            with self.subTest(label):
                with open(self.config, "w") as fh:
                    fh.write(text)
                rc, _, err = self.run_cli("config")
                self.assertEqual(rc, 2)
                self.assertEqual(len(err.strip().splitlines()), 1, err)
                self.assertTrue(err.startswith("error: config: "), err)
                self.assertIn(expect, err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
