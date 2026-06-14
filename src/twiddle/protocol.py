"""ATORCH USB/DC tester BLE protocol: frame decode + command build.

Pure functions, no I/O and no BLE dependency, so this module is trivially
testable without hardware. References:
  - https://github.com/syssi/esphome-atorch-dl24 (report frame field map)
  - https://github.com/NiceLabs/atorch-console   (command frame + reset codes)

BLE transport (same for every ATORCH UD18-class meter):
  service        0000ffe0-0000-1000-8000-00805f9b34fb
  characteristic 0000ffe1-0000-1000-8000-00805f9b34fb   (notify + write)

The meter auto-streams ~1 Hz report frames on FFE1 once you subscribe to
notifications; no "start" command is required. Commands (e.g. counter reset)
are written to the same FFE1 characteristic.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

MAGIC = b"\xff\x55"
FRAME_LEN = 36

MSG_REPORT = 0x01
MSG_COMMAND = 0x11

DEV_AC = 0x01
DEV_DC = 0x02
DEV_USB = 0x03
DEV_NAMES = {DEV_AC: "AC", DEV_DC: "DC", DEV_USB: "USB"}

# Reset command codes (written as the "command code" byte of a 0x11 frame).
RESET_CODES = {
    "wh": 0x01,        # energy (W·h)
    "ah": 0x02,        # capacity (A·h)
    "duration": 0x03,  # runtime
    "all": 0x05,       # everything
}


def _u(b: bytes) -> int:
    """Big-endian unsigned int."""
    v = 0
    for x in b:
        v = (v << 8) | x
    return v


def checksum(body: bytes) -> int:
    """ATORCH checksum: (sum(body) & 0xFF) XOR 0x44.

    `body` is the frame WITHOUT the 2-byte magic header and WITHOUT the
    trailing checksum byte (i.e. frame[2:-1]).
    """
    return (sum(body) & 0xFF) ^ 0x44


def verify_checksum(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    return checksum(frame[2:-1]) == frame[-1]


@dataclass
class Reading:
    """One decoded report frame. Fields that don't apply to a device type
    are left as None."""
    device_type: str           # "USB" | "DC" | "AC"
    voltage: float | None = None       # V
    current: float | None = None       # A
    power: float | None = None         # W (computed if not transmitted)
    capacity_ah: float | None = None   # A·h
    energy_wh: float | None = None     # W·h
    usb_dminus: float | None = None    # V
    usb_dplus: float | None = None     # V
    temperature_c: int | None = None   # °C
    runtime_s: int | None = None       # seconds
    checksum_ok: bool = True
    raw: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items()}


def find_frames(buffer: bytearray) -> list[bytes]:
    """Pull complete 36-byte frames out of a rolling notification buffer.

    Mutates `buffer` in place, removing consumed bytes. Returns the list of
    complete frames found (may be empty). Resilient to partial/teared
    notifications and to leading garbage.
    """
    frames: list[bytes] = []
    while True:
        i = buffer.find(MAGIC)
        if i < 0:
            # keep only a trailing partial-magic byte
            if buffer and buffer[-1] == MAGIC[0]:
                del buffer[:-1]
            else:
                buffer.clear()
            break
        if len(buffer) - i < FRAME_LEN:
            del buffer[:i]  # drop leading garbage, keep the partial frame
            break
        frames.append(bytes(buffer[i:i + FRAME_LEN]))
        del buffer[:i + FRAME_LEN]
    return frames


def decode_report(frame: bytes) -> Reading | None:
    """Decode a report frame (FF 55 01 ...). Returns None if not a report."""
    if len(frame) < FRAME_LEN or frame[0:2] != MAGIC or frame[2] != MSG_REPORT:
        return None
    dev = frame[3]
    r = Reading(device_type=DEV_NAMES.get(dev, f"0x{dev:02x}"),
                checksum_ok=verify_checksum(frame),
                raw=frame.hex())
    if dev == DEV_USB:
        r.voltage = _u(frame[4:7]) / 100
        r.current = _u(frame[7:10]) / 100
        r.capacity_ah = _u(frame[10:13]) / 1000
        r.energy_wh = _u(frame[13:17]) / 100
        r.usb_dminus = _u(frame[17:19]) / 100
        r.usb_dplus = _u(frame[19:21]) / 100
        r.temperature_c = _u(frame[21:23])
        r.runtime_s = _u(frame[23:25]) * 3600 + frame[25] * 60 + frame[26]
    elif dev == DEV_DC:
        r.voltage = _u(frame[4:7]) / 10
        r.current = _u(frame[7:10]) / 1000
        r.power = _u(frame[10:13]) / 10
        r.energy_wh = _u(frame[13:17]) / 100
        r.temperature_c = _u(frame[24:26])
        r.runtime_s = _u(frame[26:28]) * 3600 + frame[28] * 60 + frame[29]
    elif dev == DEV_AC:
        r.voltage = _u(frame[4:7]) / 10
        r.current = _u(frame[7:10]) / 1000
        r.power = _u(frame[10:13]) / 10
        r.energy_wh = _u(frame[13:17]) / 100
        r.temperature_c = _u(frame[24:26])
        r.runtime_s = _u(frame[26:28]) * 3600 + frame[28] * 60 + frame[29]
    if r.power is None and r.voltage is not None and r.current is not None:
        r.power = round(r.voltage * r.current, 3)
    return r


def build_command(code: int, device_type: int = DEV_USB, value: int = 0) -> bytes:
    """Build a command frame: FF 55 11 <devtype> <code> <value:4 BE> <cksum>."""
    body = bytes([MSG_COMMAND, device_type & 0xFF, code & 0xFF]) + value.to_bytes(4, "big")
    return MAGIC + body + bytes([checksum(body)])


def build_reset(counter: str = "all", device_type: int = DEV_USB) -> bytes:
    """Build a reset command. `counter` in {'all','wh','ah','duration'}."""
    key = counter.lower()
    if key not in RESET_CODES:
        raise ValueError(f"unknown counter {counter!r}; choose from {sorted(RESET_CODES)}")
    return build_command(RESET_CODES[key], device_type=device_type)
