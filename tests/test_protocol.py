"""Hardware-free tests for the ATORCH protocol codec.

Run with either:
    python -m pytest
    python tests/test_protocol.py     # falls back to a plain runner
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from twiddle import protocol as P  # noqa: E402

# Real frame captured from a bench tester (USB-type, device 0x03):
#   7.72 V, 0.82 A, 5.568 Ah, 33.07 Wh, D-/D+ 2.68/2.70 V, runtime 8h45m59s
USB_FRAME = bytes.fromhex(
    "ff5501030003040000520015c000000ceb010c010e000000082d3b3c00000000000000b5")


class TestChecksum(unittest.TestCase):
    def test_report_checksum_ok(self):
        self.assertTrue(P.verify_checksum(USB_FRAME))

    def test_corrupt_checksum_fails(self):
        bad = bytearray(USB_FRAME); bad[10] ^= 0xFF
        self.assertFalse(P.verify_checksum(bytes(bad)))


class TestDecode(unittest.TestCase):
    def test_decode_usb(self):
        r = P.decode_report(USB_FRAME)
        self.assertIsNotNone(r)
        self.assertEqual(r.device_type, "USB")
        self.assertAlmostEqual(r.voltage, 7.72, places=2)
        self.assertAlmostEqual(r.current, 0.82, places=2)        # /100, not /1000
        self.assertAlmostEqual(r.capacity_ah, 5.568, places=3)
        self.assertAlmostEqual(r.energy_wh, 33.07, places=2)
        self.assertAlmostEqual(r.usb_dminus, 2.68, places=2)
        self.assertAlmostEqual(r.usb_dplus, 2.70, places=2)
        self.assertEqual(r.runtime_s, 8 * 3600 + 45 * 60 + 59)
        self.assertTrue(r.checksum_ok)

    def test_power_computed(self):
        r = P.decode_report(USB_FRAME)
        self.assertAlmostEqual(r.power, round(7.72 * 0.82, 3), places=3)

    def test_non_report_returns_none(self):
        self.assertIsNone(P.decode_report(b"\x00" * 36))


class TestFraming(unittest.TestCase):
    def test_extracts_frame_amid_noise(self):
        buf = bytearray(b"\x01\x02" + USB_FRAME + b"\xff")  # garbage + frame + partial magic
        frames = P.find_frames(buf)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], USB_FRAME)
        self.assertEqual(bytes(buf), b"\xff")  # partial magic retained

    def test_split_notification_reassembly(self):
        buf = bytearray()
        a, b = USB_FRAME[:20], USB_FRAME[20:]
        self.assertEqual(P.find_frames(bytearray(buf + a)), [])  # incomplete
        buf.extend(a); buf.extend(b)
        self.assertEqual(P.find_frames(buf), [USB_FRAME])


class TestCommands(unittest.TestCase):
    def test_reset_all_usb(self):
        # documented frame for USB device type 0x03
        self.assertEqual(P.build_reset("all", P.DEV_USB).hex(), "ff5511030500000000" + "5d")

    def test_reset_all_generic(self):
        # documented example from NiceLabs/atorch-console uses device type 0x01
        self.assertEqual(P.build_reset("all", P.DEV_AC).hex(), "ff5511010500000000" + "53")

    def test_reset_codes(self):
        self.assertEqual(P.build_reset("wh", P.DEV_USB)[4], 0x01)
        self.assertEqual(P.build_reset("ah", P.DEV_USB)[4], 0x02)
        self.assertEqual(P.build_reset("duration", P.DEV_USB)[4], 0x03)

    def test_bad_counter(self):
        with self.assertRaises(ValueError):
            P.build_reset("nope")

    def test_command_checksum_roundtrips(self):
        frame = P.build_reset("all", P.DEV_USB)
        self.assertEqual(P.checksum(frame[2:-1]), frame[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
