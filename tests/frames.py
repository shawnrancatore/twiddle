"""Synthetic ATORCH report frames for tests, built from docs/PROTOCOL.md.

Deliberately independent of twiddle.protocol, so a test can check the codec
against the documented layout rather than against itself.
"""

FRAME_LEN = 36
DEV_AC, DEV_DC, DEV_USB = 0x01, 0x02, 0x03


def checksum(body: bytes) -> int:
    """PROTOCOL.md: (sum(frame[2:-1]) & 0xFF) XOR 0x44."""
    return (sum(body) & 0xFF) ^ 0x44


def report(dev_type: int, fields: dict[int, tuple[int, int]] | None = None) -> bytes:
    """A 36-byte report frame `FF 55 01 <dev_type> ... <checksum>`.

    `fields` maps a documented offset to (size in bytes, unsigned value),
    written big-endian. Every other byte is zero.
    """
    frame = bytearray(FRAME_LEN)
    frame[0:4] = bytes([0xFF, 0x55, 0x01, dev_type])
    for offset, (size, value) in (fields or {}).items():
        frame[offset:offset + size] = value.to_bytes(size, "big")
    frame[-1] = checksum(frame[2:-1])
    return bytes(frame)
