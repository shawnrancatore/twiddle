"""Freshness of a meter's status, and what the manager logs. No Bluetooth:
`bleak` is replaced by a fake module that connects instantly."""
import asyncio
import logging
import os
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from twiddle import client as C  # noqa: E402
from twiddle.config import BleTuning  # noqa: E402

import frames  # noqa: E402

FRAME = frames.report(frames.DEV_USB, {0x04: (3, 491)})


def manager(**tuning):
    return C.DeviceManager("meter-a", "AA:BB:CC:DD:EE:01", BleTuning(**tuning))


class TestAge(unittest.TestCase):
    def test_first_connect_without_a_frame_has_no_age(self):
        m = manager()
        m._mark_connected()
        st = m.status()
        self.assertIsNone(st["reading"])
        self.assertIsNone(st["age_s"])

    def test_a_frame_makes_the_age_small(self):
        m = manager()
        m._mark_connected()
        m._on_notify(None, bytearray(FRAME))
        st = m.status()
        self.assertIsNotNone(st["reading"])
        self.assertLess(st["age_s"], 1.0)

    def test_reconnect_without_a_frame_keeps_the_old_age_growing(self):
        m = manager()
        m._mark_connected()
        m._on_notify(None, bytearray(FRAME))
        m.last_frame_ts = time.time() - 10  # the frame is 10 s old
        m.connected = False
        m._mark_connected()                 # reconnected, no new frame yet
        self.assertGreaterEqual(m.status()["age_s"], 10.0)

    def test_watchdog_still_gets_a_grace_period_after_connect(self):
        m = manager(stale_timeout=12.0)
        m.last_frame_ts = time.time() - 100  # an old frame from before the drop
        m._mark_connected()
        self.assertFalse(m._stale())

    def test_watchdog_fires_when_no_frame_comes(self):
        m = manager(stale_timeout=12.0)
        m._mark_connected()
        m.connected_at = time.time() - 13
        self.assertTrue(m._stale())


class FakeBleakClient:
    def __init__(self, dev, timeout=None, disconnected_callback=None):
        self.is_connected = False
        self.cb = disconnected_callback

    async def connect(self):
        self.is_connected = True

    async def start_notify(self, _char, _cb):
        pass

    async def disconnect(self):
        self.is_connected = False


class FakeScanner:
    @staticmethod
    async def find_device_by_address(mac, timeout=None):
        return object()


class TestLogging(unittest.TestCase):
    def test_a_dropped_link_is_logged_at_info(self):
        fake = types.SimpleNamespace(BleakClient=FakeBleakClient, BleakScanner=FakeScanner)

        async def go():
            m = manager(reconnect_delay=0.01)
            task = asyncio.create_task(m.run())
            for _ in range(100):
                await asyncio.sleep(0.01)
                if m.connected:
                    break
            m._disconnected.set()  # the link drops
            for _ in range(300):  # the writer loop notices within a second
                await asyncio.sleep(0.01)
                if m.connect_count == 2:
                    break
            m.stop()
            await asyncio.wait_for(task, 3)

        with mock.patch.dict(sys.modules, {"bleak": fake}), \
                self.assertLogs("twiddle.client", logging.INFO) as logs:
            asyncio.run(go())
        self.assertIn("INFO:twiddle.client:meter-a: disconnected", logs.output)

    def test_a_reset_that_was_written_is_logged_without_the_bytes(self):
        async def go():
            m = manager(command_timeout=1.0)
            m.connected = True
            m.device_type = "USB"
            m.last_frame_ts = 1e12
            client = types.SimpleNamespace(is_connected=True, writes=[])

            async def write(_c, data, response=False):
                client.writes.append(bytes(data))
            client.write_gatt_char = write
            serve = asyncio.create_task(m._serve(client))
            await m.reset("wh")
            m.stop()
            await serve
            return client.writes

        with self.assertLogs("twiddle.client", logging.INFO) as logs:
            writes = asyncio.run(go())
        self.assertEqual(len(writes), 1)
        self.assertEqual(logs.output, ["INFO:twiddle.client:meter-a: reset wh sent"])

    def test_a_refused_reset_is_not_logged_as_sent(self):
        async def go():
            m = manager()
            m.connected = True  # no frame yet, so the type is unknown
            with self.assertRaises(RuntimeError):
                await m.reset("wh")

        with self.assertNoLogs("twiddle.client", logging.INFO):
            asyncio.run(go())


if __name__ == "__main__":
    unittest.main(verbosity=2)
