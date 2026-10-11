"""Hardware-free tests for the ATORCH protocol codec.

Run with either:
    python -m pytest
    python tests/test_protocol.py     # falls back to a plain runner
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from twiddle import protocol as P  # noqa: E402

import frames  # noqa: E402

# A real frame captured from a USB meter (device type 0x03):
#   7.72 V, 0.82 A, 5.568 Ah, 33.07 Wh, D-/D+ 2.68/2.70 V, runtime 8h45m59s
USB_FRAME = bytes.fromhex(
    "ff5501030003040000520015c000000ceb010c010e000000082d3b3c00000000000000b5")

# Real frames from two ATORCH UD18 (USB-type) meters, as `twiddle get --json`
# printed them. Neither meter fills the temperature bytes.
UD18_A_FRAME = bytes.fromhex(
    "ff5501030001eb0000040378880003169f000500120000031b1c273c0000000000000027")
UD18_B_FRAME = bytes.fromhex(
    "ff5501030001eb0000070004ba0000026700050012000000032f323c0000000000000091")


class TestRealUD18Frames(unittest.TestCase):
    def test_meter_a(self):
        r = P.decode_report(UD18_A_FRAME)
        self.assertEqual(r.device_type, "USB")
        self.assertAlmostEqual(r.voltage, 4.91, places=2)
        self.assertAlmostEqual(r.current, 0.04, places=2)
        self.assertAlmostEqual(r.power, 0.196, places=3)
        self.assertAlmostEqual(r.capacity_ah, 227.464, places=3)
        self.assertAlmostEqual(r.energy_wh, 2023.99, places=2)
        self.assertAlmostEqual(r.usb_dminus, 0.05, places=2)
        self.assertAlmostEqual(r.usb_dplus, 0.18, places=2)
        self.assertEqual(r.temperature_c, 0)
        self.assertEqual(r.runtime_s, 795 * 3600 + 28 * 60 + 39)
        self.assertTrue(r.checksum_ok)

    def test_meter_b(self):
        r = P.decode_report(UD18_B_FRAME)
        self.assertEqual(r.device_type, "USB")
        self.assertAlmostEqual(r.voltage, 4.91, places=2)
        self.assertAlmostEqual(r.current, 0.07, places=2)
        self.assertAlmostEqual(r.power, 0.344, places=3)
        self.assertAlmostEqual(r.capacity_ah, 1.21, places=3)
        self.assertAlmostEqual(r.energy_wh, 6.15, places=2)
        self.assertAlmostEqual(r.usb_dminus, 0.05, places=2)
        self.assertAlmostEqual(r.usb_dplus, 0.18, places=2)
        self.assertEqual(r.temperature_c, 0)
        self.assertEqual(r.runtime_s, 3 * 3600 + 47 * 60 + 50)
        self.assertTrue(r.checksum_ok)


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


class TestDecodeDC(unittest.TestCase):
    """A synthetic DC frame (devType 0x02) built from PROTOCOL.md's offsets."""

    FRAME = frames.report(frames.DEV_DC, {
        0x04: (3, 123),    # voltage   ÷10   -> 12.3 V
        0x07: (3, 1234),   # current   ÷1000 -> 1.234 A
        0x0A: (3, 151),    # power     ÷10   -> 15.1 W
        0x0D: (4, 4567),   # energy    ÷100  -> 45.67 W·h
        0x11: (3, 250),    # price     ÷100  (not decoded)
        0x18: (2, 31),     # temperature     -> 31 °C
        0x1A: (2, 2),      # hours
        0x1C: (1, 3),      # minutes
        0x1D: (1, 4),      # seconds         -> 2h03m04s
    })

    def test_decode_dc(self):
        r = P.decode_report(self.FRAME)
        self.assertEqual(r.device_type, "DC")
        self.assertAlmostEqual(r.voltage, 12.3, places=3)
        self.assertAlmostEqual(r.current, 1.234, places=3)       # /1000, not /100
        self.assertAlmostEqual(r.energy_wh, 45.67, places=2)
        self.assertEqual(r.temperature_c, 31)
        self.assertEqual(r.runtime_s, 2 * 3600 + 3 * 60 + 4)
        self.assertTrue(r.checksum_ok)

    def test_dc_power_comes_from_the_frame(self):
        r = P.decode_report(self.FRAME)
        self.assertAlmostEqual(r.power, 15.1, places=3)          # not 12.3 * 1.234

    def test_dc_has_no_charge_or_usb_fields(self):
        # PROTOCOL.md documents no capacity (A·h) field for DC meters
        r = P.decode_report(self.FRAME)
        self.assertIsNone(r.capacity_ah)
        self.assertIsNone(r.usb_dminus)
        self.assertIsNone(r.usb_dplus)


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
        self.assertEqual(P.build_reset("all", device_type=P.DEV_USB).hex(), "ff5511030500000000" + "5d")

    def test_reset_all_generic(self):
        # documented example from NiceLabs/atorch-console uses device type 0x01
        self.assertEqual(P.build_reset("all", device_type=P.DEV_AC).hex(), "ff5511010500000000" + "53")

    def test_reset_codes(self):
        self.assertEqual(P.build_reset("wh", device_type=P.DEV_USB)[4], 0x01)
        self.assertEqual(P.build_reset("ah", device_type=P.DEV_USB)[4], 0x02)
        self.assertEqual(P.build_reset("duration", device_type=P.DEV_USB)[4], 0x03)

    def test_bad_counter(self):
        with self.assertRaises(ValueError):
            P.build_reset("nope", device_type=P.DEV_USB)

    def test_device_type_is_a_required_keyword(self):
        # no default type: a reset with the wrong type is the bug this prevents
        for call in (lambda: P.build_reset(),
                     lambda: P.build_reset("all"),
                     lambda: P.build_reset("all", P.DEV_DC),
                     lambda: P.build_command(0x05),
                     lambda: P.build_command(0x05, P.DEV_DC)):
            with self.assertRaises(TypeError):
                call()
        self.assertEqual(P.build_command(0x05, device_type=P.DEV_DC),
                         P.build_reset(device_type=P.DEV_DC))

    def test_command_checksum_roundtrips(self):
        frame = P.build_reset("all", device_type=P.DEV_USB)
        self.assertEqual(P.checksum(frame[2:-1]), frame[-1])


class TestResetBytes(unittest.TestCase):
    """build_reset against PROTOCOL.md's command frame for every meter type:
    FF 55 11 <devType> <code> <value:4 BE> <checksum>, with codes 01 W·h,
    02 A·h, 03 runtime, 05 all, and checksum (sum(frame[2:-1]) & 0xFF) ^ 0x44.
    """

    EXPECTED = {
        # (device type, counter): frame
        (P.DEV_AC, "wh"): "ff 55 11 01 01 00 00 00 00 57",
        (P.DEV_AC, "ah"): "ff 55 11 01 02 00 00 00 00 50",
        (P.DEV_AC, "duration"): "ff 55 11 01 03 00 00 00 00 51",
        (P.DEV_AC, "all"): "ff 55 11 01 05 00 00 00 00 53",  # PROTOCOL.md example
        (P.DEV_DC, "wh"): "ff 55 11 02 01 00 00 00 00 50",
        (P.DEV_DC, "ah"): "ff 55 11 02 02 00 00 00 00 51",
        (P.DEV_DC, "duration"): "ff 55 11 02 03 00 00 00 00 52",
        (P.DEV_DC, "all"): "ff 55 11 02 05 00 00 00 00 5c",
        (P.DEV_USB, "wh"): "ff 55 11 03 01 00 00 00 00 51",
        (P.DEV_USB, "ah"): "ff 55 11 03 02 00 00 00 00 52",
        (P.DEV_USB, "duration"): "ff 55 11 03 03 00 00 00 00 53",
        (P.DEV_USB, "all"): "ff 55 11 03 05 00 00 00 00 5d",  # PROTOCOL.md example
    }

    def test_documented_bytes(self):
        for (dev, counter), expected in self.EXPECTED.items():
            with self.subTest(dev=dev, counter=counter):
                self.assertEqual(P.build_reset(counter, device_type=dev), bytes.fromhex(expected))

    def test_checksum_is_documented_formula(self):
        for (dev, counter) in self.EXPECTED:
            with self.subTest(dev=dev, counter=counter):
                frame = P.build_reset(counter, device_type=dev)
                self.assertEqual(frame[-1], (sum(frame[2:-1]) & 0xFF) ^ 0x44)
                self.assertTrue(P.verify_checksum(frame))

    def test_device_type_codes_match_protocol(self):
        self.assertEqual((P.DEV_AC, P.DEV_DC, P.DEV_USB), (0x01, 0x02, 0x03))


if __name__ == "__main__":
    unittest.main(verbosity=2)
