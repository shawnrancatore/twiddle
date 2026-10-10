"""A reset must carry the meter's own device type, learned from its last
report frame, on both the one-shot CLI path and the service path.

No Bluetooth: the one-shot path talks to a fake client, and the service path
captures the command instead of queueing it for a connection.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from twiddle import client as C  # noqa: E402
from twiddle import protocol as P  # noqa: E402
from twiddle.config import BleTuning  # noqa: E402

import frames  # noqa: E402

USB_FRAME = bytes.fromhex(
    "ff5501030003040000520015c000000ceb010c010e000000082d3b3c00000000000000b5")
DC_FRAME = frames.report(frames.DEV_DC)
AC_FRAME = frames.report(frames.DEV_AC)
ODD_FRAME = frames.report(0x07)  # a device type PROTOCOL.md does not list
# A DC frame whose devType byte flipped to USB in transit: it decodes as USB,
# but its checksum no longer matches.
CORRUPT = bytearray(DC_FRAME)
CORRUPT[3] = frames.DEV_USB
CORRUPT = bytes(CORRUPT)

RESET_ALL = {
    "USB": bytes.fromhex("ff 55 11 03 05 00 00 00 00 5d"),
    "DC": bytes.fromhex("ff 55 11 02 05 00 00 00 00 5c"),
    "AC": bytes.fromhex("ff 55 11 01 05 00 00 00 00 53"),
}


class FakeClient:
    """Stands in for a connected BleakClient. On subscribe it delivers the
    given notifications, the way a meter streams report frames."""

    def __init__(self, notifications):
        self.notifications = notifications
        self.writes: list[bytes] = []
        self.disconnected = False
        self.is_connected = True

    async def start_notify(self, _char, callback):
        for data in self.notifications:
            callback(_char, bytearray(data))

    async def write_gatt_char(self, _char, data, response=False):
        self.writes.append(bytes(data))

    async def disconnect(self):
        self.disconnected = True


def run_one_shot(notifications, counter="all", settle=6.0):
    fake = FakeClient(notifications)

    async def connect(_mac, _ble, _attempts):
        return fake

    with mock.patch.object(C, "_connect_retry", connect):
        asyncio.run(C.reset_once("AA:BB:CC:DD:EE:01", counter, BleTuning(),
                                 settle=settle))
    return fake


class TestOneShotReset(unittest.TestCase):
    def test_usb_meter_gets_usb_frame(self):
        fake = run_one_shot([USB_FRAME])
        self.assertEqual(fake.writes, [RESET_ALL["USB"]])
        self.assertTrue(fake.disconnected)

    def test_dc_meter_gets_dc_frame(self):
        fake = run_one_shot([DC_FRAME])
        self.assertEqual(fake.writes, [RESET_ALL["DC"]])

    def test_ac_meter_gets_ac_frame(self):
        fake = run_one_shot([AC_FRAME])
        self.assertEqual(fake.writes, [RESET_ALL["AC"]])

    def test_counter_is_respected(self):
        fake = run_one_shot([DC_FRAME], counter="wh")
        self.assertEqual(fake.writes, [bytes.fromhex("ff 55 11 02 01 00 00 00 00 50")])

    def test_frame_split_across_notifications(self):
        fake = run_one_shot([DC_FRAME[:20], DC_FRAME[20:]])
        self.assertEqual(fake.writes, [RESET_ALL["DC"]])

    def test_no_frame_sends_nothing(self):
        with self.assertRaises(TimeoutError):
            run_one_shot([], settle=0.2)

    def test_no_frame_still_disconnects(self):
        fake = FakeClient([])

        async def connect(_mac, _ble, _attempts):
            return fake

        with mock.patch.object(C, "_connect_retry", connect):
            with self.assertRaises(TimeoutError):
                asyncio.run(C.reset_once("AA:BB:CC:DD:EE:01", "all", BleTuning(),
                                         settle=0.2))
        self.assertEqual(fake.writes, [])
        self.assertTrue(fake.disconnected)

    def test_unknown_device_type_sends_nothing(self):
        fake = FakeClient([ODD_FRAME])

        async def connect(_mac, _ble, _attempts):
            return fake

        with mock.patch.object(C, "_connect_retry", connect):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(C.reset_once("AA:BB:CC:DD:EE:01", "all", BleTuning()))
        self.assertIn("0x07", str(ctx.exception))
        self.assertEqual(fake.writes, [])
        self.assertTrue(fake.disconnected)

    def test_corrupt_frames_are_skipped(self):
        fake = run_one_shot([CORRUPT, DC_FRAME, CORRUPT])
        self.assertEqual(fake.writes, [RESET_ALL["DC"]])

    def test_only_corrupt_frames_sends_nothing(self):
        fake = FakeClient([CORRUPT, CORRUPT])

        async def connect(_mac, _ble, _attempts):
            return fake

        with mock.patch.object(C, "_connect_retry", connect):
            with self.assertRaises(TimeoutError) as ctx:
                asyncio.run(C.reset_once("AA:BB:CC:DD:EE:01", "all", BleTuning(),
                                         settle=0.2))
        self.assertIn("valid checksum", str(ctx.exception))
        self.assertEqual(fake.writes, [])
        self.assertTrue(fake.disconnected)

    def test_bad_counter_never_connects(self):
        async def connect(*_a):
            raise AssertionError("connected for a bad counter")

        with mock.patch.object(C, "_connect_retry", connect):
            with self.assertRaises(ValueError):
                asyncio.run(C.reset_once("AA:BB:CC:DD:EE:01", "nope", BleTuning()))


class TestServiceReset(unittest.TestCase):
    def manager(self, *notifications):
        """A connected manager that has received these notifications."""
        m = C.DeviceManager("meter-a", "AA:BB:CC:DD:EE:01", BleTuning())
        m.connected = True
        for data in notifications:
            m._on_notify(None, bytearray(data))
        m.sent = []

        async def capture(cmd):
            m.sent.append(cmd)

        m.send_command = capture
        return m

    def test_usb(self):
        m = self.manager(USB_FRAME)
        asyncio.run(m.reset("all"))
        self.assertEqual(m.sent, [RESET_ALL["USB"]])

    def test_dc(self):
        m = self.manager(DC_FRAME)
        asyncio.run(m.reset("all"))
        self.assertEqual(m.sent, [RESET_ALL["DC"]])

    def test_ac(self):
        m = self.manager(AC_FRAME)
        asyncio.run(m.reset("all"))
        self.assertEqual(m.sent, [RESET_ALL["AC"]])

    def test_no_frame_yet_refuses(self):
        m = self.manager()
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(m.reset("all"))
        self.assertIn("no report frame", str(ctx.exception))
        self.assertEqual(m.sent, [])

    def test_corrupt_frame_does_not_change_the_type(self):
        self.assertFalse(P.verify_checksum(CORRUPT))
        self.assertEqual(P.decode_report(CORRUPT).device_type, "USB")
        m = self.manager(DC_FRAME, CORRUPT)
        self.assertFalse(m.last_reading.checksum_ok)  # still shown, flagged
        asyncio.run(m.reset("all"))
        self.assertEqual(m.sent, [RESET_ALL["DC"]])

    def test_only_corrupt_frames_refuses(self):
        m = self.manager(CORRUPT)
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(m.reset("all"))
        self.assertIn("valid checksum", str(ctx.exception))
        self.assertEqual(m.sent, [])

    def test_unknown_device_type_refuses(self):
        m = self.manager(ODD_FRAME)
        with self.assertRaises(RuntimeError):
            asyncio.run(m.reset("all"))
        self.assertEqual(m.sent, [])


class GatedClient(FakeClient):
    """A connection whose first write waits until the test lets it go."""

    def __init__(self):
        super().__init__([])
        self.gate = asyncio.Event()
        self.first = True

    async def write_gatt_char(self, _char, data, response=False):
        if self.first:
            self.first = False
            await self.gate.wait()
        self.writes.append(bytes(data))


def connected_manager(**tuning):
    m = C.DeviceManager("meter-a", "AA:BB:CC:DD:EE:01",
                        BleTuning(command_timeout=0.1, **tuning))
    m.connected = True
    m.last_frame_ts = 1e12  # the stream never looks stale
    return m


class TestQueuedCommands(unittest.TestCase):
    def test_timed_out_command_is_never_written(self):
        async def go():
            m, client = connected_manager(), GatedClient()
            serve = asyncio.create_task(m._serve(client))
            first = asyncio.get_running_loop().create_future()
            m._cmd_q.put_nowait(C.Command(b"first", first))
            await asyncio.sleep(0.01)  # the writer is now blocked on `first`
            with self.assertRaises(TimeoutError) as cm:
                await m.send_command(b"late")
            self.assertIn("reset was not sent", str(cm.exception))
            client.gate.set()
            await first
            await asyncio.sleep(0.1)
            m.stop()
            await serve
            return client.writes
        self.assertEqual(asyncio.run(go()), [b"first"])

    def test_timeout_during_a_write_says_it_may_still_be_applied(self):
        async def go():
            m, client = connected_manager(), GatedClient()
            serve = asyncio.create_task(m._serve(client))
            with self.assertRaises(TimeoutError) as cm:
                await m.send_command(b"hung")  # the write itself hangs
            client.gate.set()
            await asyncio.sleep(0.05)
            m.stop()
            await serve
            return str(cm.exception)
        msg = asyncio.run(go())
        self.assertIn("reset may still be applied", msg)
        self.assertIn("read the meter before you send it again", msg)

    def test_commands_queued_before_a_disconnect_are_failed(self):
        async def go():
            m = connected_manager()
            pending = asyncio.create_task(m.send_command(b"old"))
            await asyncio.sleep(0.01)
            m._disconnected.set()
            await m._serve(FakeClient([]))  # the connection is gone
            with self.assertRaises(ConnectionError) as cm:
                await pending
            self.assertIn("meter-a disconnected before the command was sent",
                          str(cm.exception))
            # reconnected: the old command must not be written
            m._disconnected.clear()
            again = FakeClient([])
            serve = asyncio.create_task(m._serve(again))
            await asyncio.sleep(1.2)
            m.stop()
            await serve
            return again.writes
        self.assertEqual(asyncio.run(go()), [])

    def test_normal_command_writes_one_frame(self):
        async def go():
            m, client = connected_manager(), FakeClient([])
            serve = asyncio.create_task(m._serve(client))
            await m.send_command(b"one")
            m.stop()
            await serve
            return client.writes
        self.assertEqual(asyncio.run(go()), [b"one"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
