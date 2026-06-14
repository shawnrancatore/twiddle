# twiddle

Reliable **service + CLI** for reading and controlling **ATORCH USB/DC BLE
testers** (UD18 / DL24 class meters — the ones that expose BLE serial service
`FFE0`). It gives each meter a friendly name, keeps a resilient always-on
connection, exposes live voltage/current/power/energy, and can clear the
device counters.

```
$ twiddle list
twdlDEE  78:5F:2D:C4:0C:BD  online  (servo / PWM power bus)  [0.3s ago]
    USB  V=8.02V  I=0.14A  P=1.12W
    cap=25.657Ah  energy=298.99Wh  runtime=0h48m27s
twdlDUM  D9:9B:58:F4:F1:0B  online  (logic / Pi rail)  [0.2s ago]
    USB  V=7.66V  I=0.88A  P=6.74W
    cap=6.135Ah  energy=37.42Wh  runtime=9h24m01s
```

> **Agents:** see [`AGENTS.md`](AGENTS.md) for a machine-oriented usage guide
> (commands, JSON schemas, exit codes). Tool-specific entry points:
> [`CLAUDE.md`](CLAUDE.md), [`GEMINI.md`](GEMINI.md).

## Why a service?

These meters use a cheap HM-10-style BLE module that:

1. **allows only one connection at a time** (one central owns the radio), and
2. **is connection-flaky** — BlueZ frequently returns
   `le-connection-abort-by-local` / timeouts, and the module needs a few
   seconds to re-advertise after each disconnect.

The vendor phone app feels rock-solid only because its BLE library retries and
keeps a single long-lived link. `twiddle` does the same: a small service owns
**one persistent connection per meter**, self-heals on drops, and answers the
CLI (and any agents) over a local socket — so nothing fights over the radio.

## Install

Requires Python ≥ 3.11 and a working BlueZ stack (Linux). On a Raspberry Pi the
user must be in the `bluetooth` group (or run the service via systemd).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # installs the `twiddle` command + bleak
```

## Quick start

```bash
cp config.example.toml twiddle.toml      # edit device names/MACs if needed
twiddle scan                              # discover ATORCH (FFE0) devices + map names
twiddle service                           # run the service (foreground; or use systemd)
# in another shell:
twiddle list                              # all devices + latest readings
twiddle get twdlDEE --json                # one device as JSON
twiddle watch twdlDEE                      # live updating reading
twiddle reset twdlDUM --counter all        # clear counters (asks to confirm)
```

If **no service is running**, `get`/`reset` fall back to a direct one-shot BLE
connection (slower, and it competes for the single connection — stop the
service first).

## Configuration

TOML, searched in this order: `$TWIDDLE_CONFIG`, `./twiddle.toml`,
`~/.config/twiddle/config.toml`, `/etc/twiddle/config.toml`. See
[`config.example.toml`](config.example.toml). Add or rename devices freely:

```toml
[devices.myMeter]
mac  = "AA:BB:CC:DD:EE:FF"
note = "what it's measuring"
```

The two bench testers are interchangeable twins, so the default
`twdlDEE`/`twdlDUM` ↔ MAC mapping is a starting guess — run `twiddle scan` and
swap the MACs if a name lands on the wrong unit.

## Run as a systemd service

```bash
sudo cp systemd/twiddle.service /etc/systemd/system/
sudo cp config.example.toml /etc/twiddle/config.toml   # edit as needed
sudo systemctl enable --now twiddle
twiddle --socket /run/twiddle/twiddle.sock list
```

## Optional HTTP/JSON endpoint

Set `http_port` in `[service]` to expose read-only JSON on localhost:

```bash
curl -s http://127.0.0.1:8787/           # all devices
curl -s http://127.0.0.1:8787/device/twdlDEE
```

## Reading fields

| field | meaning | unit |
|---|---|---|
| `voltage` | bus voltage | V |
| `current` | bus current | A |
| `power` | power (computed if not sent) | W |
| `capacity_ah` | accumulated charge | A·h |
| `energy_wh` | accumulated energy | W·h |
| `usb_dplus`/`usb_dminus` | USB data-line voltages (USB meters) | V |
| `temperature_c` | meter temperature | °C |
| `runtime_s` | accumulated runtime | s |
| `checksum_ok` | frame checksum validated | bool |

## Resetting counters

`twiddle reset <device> --counter {all,wh,ah,duration}` writes a command frame
to the meter. **This is irreversible** and wipes the accumulated Ah/Wh/runtime;
the CLI asks for confirmation unless you pass `-y`.

## Testing (no hardware)

```bash
python -m pytest          # or: python tests/test_protocol.py
```

The protocol codec is pure functions with no BLE dependency, validated against
a real captured frame and the documented reset commands.

## Protocol

See [`docs/PROTOCOL.md`](docs/PROTOCOL.md). Credit to the prior reverse
engineering at
[syssi/esphome-atorch-dl24](https://github.com/syssi/esphome-atorch-dl24) and
[NiceLabs/atorch-console](https://github.com/NiceLabs/atorch-console).

## License

MIT — see [`LICENSE`](LICENSE).
