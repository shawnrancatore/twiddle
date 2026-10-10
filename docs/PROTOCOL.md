# ATORCH meter BLE protocol

Implemented in `src/twiddle/protocol.py` (pure functions, hardware-free).

## Transport

| | |
|---|---|
| Service UUID | `0000ffe0-0000-1000-8000-00805f9b34fb` |
| Characteristic UUID | `0000ffe1-0000-1000-8000-00805f9b34fb` (notify + write) |

Subscribe to notifications on the characteristic; the meter **auto-streams**
report frames at ~1 Hz. No start/handshake command and **no pairing/keys** are
required (confirmed by decompiling the vendor APK — only `ffe0`/`ffe1`, no
security). Commands (e.g. counter reset) are **written** to the same
characteristic.

## Frame structure

All frames: `FF 55 <msgType> <devType> … <checksum>`, total **36 bytes** for
reports. Multi-byte values are **big-endian unsigned**.

- `msgType`: `0x01` = report (meter → host), `0x11` = command (host → meter)
- `devType`: `0x01` AC, `0x02` DC, `0x03` USB
- `checksum` (last byte): `(sum(frame[2:-1]) & 0xFF) XOR 0x44`

### Report frame — USB meter (`devType 0x03`)

| field | offset | size | scale |
|---|---|---|---|
| voltage | 0x04 | 3 | ÷100 → V |
| current | 0x07 | 3 | **÷100 → A** |
| capacity | 0x0A | 3 | ÷1000 → A·h |
| energy | 0x0D | 4 | ÷100 → W·h |
| USB D− | 0x11 | 2 | ÷100 → V |
| USB D+ | 0x13 | 2 | ÷100 → V |
| temperature | 0x15 | 2 | °C |
| hour / min / sec | 0x17 / 0x19 / 0x1A | 2 / 1 / 1 | runtime |

> Note: USB-meter **current is ÷100**, not ÷1000. (DC meters use ÷1000 — easy
> to get wrong; that's why the codec branches on `devType`.)

### Report frame — DC meter (`devType 0x02`)

| field | offset | size | scale |
|---|---|---|---|
| voltage | 0x04 | 3 | ÷10 → V |
| current | 0x07 | 3 | ÷1000 → A |
| power | 0x0A | 3 | ÷10 → W |
| energy | 0x0D | 4 | ÷100 → W·h |
| price | 0x11 | 3 | ÷100 |
| temperature | 0x18 | 2 | °C |
| hour / min / sec | 0x1A / 0x1C / 0x1D | 2 / 1 / 1 | runtime |

### Command frame (host → meter)

`FF 55 11 <devType> <code> <value:4 BE> <checksum>`

| code | action |
|---|---|
| `0x01` | reset energy (W·h) |
| `0x02` | reset capacity (A·h) |
| `0x03` | reset runtime |
| `0x05` | reset all counters |

Checksum over `frame[2:-1]` as above. Examples:

- reset-all, USB (`devType 0x03`): `FF 55 11 03 05 00 00 00 00 5D`
- reset-all, generic (`devType 0x01`): `FF 55 11 01 05 00 00 00 00 53`

twiddle puts the meter's own `devType` in a reset, taken from that meter's last
report frame. Without a report frame, or with a `devType` not listed above, it
sends nothing.

## Validated test vector

A real captured USB frame
`ff5501030003040000520015c000000ceb010c010e000000082d3b3c00000000000000b5`
decodes to **7.72 V, 0.82 A, 5.568 A·h, 33.07 W·h, D−/D+ 2.68/2.70 V,
runtime 8h45m59s**, checksum `0xB5` ✓. See `tests/test_protocol.py`.

## References

- https://github.com/syssi/esphome-atorch-dl24 (report frame map)
- https://github.com/NiceLabs/atorch-console (command frame + reset codes)
- https://github.com/msillano/UD18-protocol-and-node-red (UD18 protocol notes)
