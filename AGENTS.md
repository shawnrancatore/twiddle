# AGENTS.md — using `twiddle` from a coding agent

Canonical machine-oriented guide for AI coding agents (OpenAI Codex, Google
Antigravity, and any agent that reads `AGENTS.md`). Claude Code and Gemini CLI
have thin pointer files (`CLAUDE.md`, `GEMINI.md`) that defer here.

`twiddle` reads & controls ATORCH USB/DC Bluetooth power meters. Prefer the
**CLI** — it is the stable, scriptable interface. Use `--json` for parsing.

## TL;DR command recipes

```bash
twiddle list --json                 # all devices + latest readings (JSON)
twiddle get <name> --json           # one device's status+reading (JSON)
twiddle scan --json                 # discover FFE0 meters in range
twiddle reset <name> --counter all -y   # clear counters (DESTRUCTIVE, see below)
twiddle config                      # effective config (source, socket, devices)
twiddle service                     # run the persistent service (foreground)
```

Device names come from config (default bench setup: `twdlDEE`, `twdlDUM`).
Never hard-code MAC addresses in code that calls this tool — use names and let
config resolve them. Run `twiddle config` to discover available names.

## Architecture (what you can rely on)

- A **service** owns one persistent BLE connection per meter and caches the
  latest reading. The **CLI** talks to it over a Unix socket
  (default `/run/twiddle/twiddle.sock`, or `$TWIDDLE_SOCKET`).
- Meters allow **only one BLE connection at a time**. Do **not** write your own
  BLE client that connects directly while the service runs — go through the
  CLI/socket/HTTP instead.
- If no service is running, `get`/`reset` transparently fall back to a slower
  one-shot direct connection.

## Exit codes

- `0` success · `1` runtime error (printed to stderr) · `2` usage/unknown device.

## JSON shapes

`twiddle get <name> --json` →
```json
{
  "name": "twdlDEE", "mac": "78:5F:2D:C4:0C:BD", "note": "servo / PWM power bus",
  "connected": true, "connect_count": 3, "fail_streak": 0,
  "last_error": null, "age_s": 0.3,
  "reading": {
    "device_type": "USB", "voltage": 8.02, "current": 0.14, "power": 1.12,
    "capacity_ah": 25.657, "energy_wh": 298.99, "usb_dminus": 2.68,
    "usb_dplus": 2.70, "temperature_c": 0, "runtime_s": 2907,
    "checksum_ok": true, "raw": "ff5501..."
  }
}
```
`reading` is `null` if no frame has been received yet. Check `connected` and
`age_s` for freshness. `twiddle list --json` → `{"ok":true,"devices":{name:status}}`.

## Optional HTTP/JSON (if `http_port` is configured)

```bash
curl -s http://127.0.0.1:8787/             # {"ok":true,"devices":{...}}
curl -s http://127.0.0.1:8787/device/twdlDEE
```

## Using the protocol library directly (Python)

Pure, hardware-free codec — safe to import in tests/tools:
```python
from twiddle import protocol as P
r = P.decode_report(frame_bytes)         # -> Reading | None
P.verify_checksum(frame_bytes)           # -> bool
P.build_reset("all", P.DEV_USB)          # -> command bytes (FF55 1103 05 .. 5D)
```
BLE specifics: service `0000ffe0-…`, characteristic `0000ffe1-…` (notify +
write); the meter auto-streams ~1 Hz report frames once you subscribe — no
start command needed.

## ⚠️ Safety — counter reset is destructive

`twiddle reset` permanently clears the meter's accumulated Ah / Wh / runtime.
**Do not run it autonomously.** Only run it when the human explicitly asked to
clear counters, and prefer to confirm which device and which counter first.
Without `-y` the CLI prompts interactively (which will block a non-interactive
agent — that friction is intentional).

## Adding / renaming devices

Edit the active config (`twiddle config` shows its `source`):
```toml
[devices.someName]
mac  = "AA:BB:CC:DD:EE:FF"
note = "free text"
```
Find MACs with `twiddle scan --json` (FFE0 advertisers; unmapped ones are
shown). Then restart the service.

## Tests & dev

```bash
python -m pytest            # hardware-free protocol tests
pip install -e .            # editable install (provides the `twiddle` command)
```
Key modules: `protocol.py` (codec, no I/O), `client.py` (resilient BLE +
one-shot helpers), `service.py` (persistent connections + socket/HTTP),
`cli.py`, `config.py`.

## Troubleshooting

- **A device shows `OFFLINE` / `fail_streak` rising:** normal for the flakier
  module; the service keeps retrying and will recover. Increase
  `[ble] clear_cache_after` aggressiveness or `connect_timeout` in config if
  persistent.
- **Everything offline:** check the adapter (`hciconfig hci0`, `rfkill list`),
  group membership (`bluetooth`), and that the phone app isn't holding the
  meters (one central at a time).
- **`twiddle list` says "no service":** start `twiddle service` (or the systemd
  unit), or rely on the direct fallback for one-shot reads.
