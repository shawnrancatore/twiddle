# AGENTS.md

A guide for coding agents that use or change `twiddle`.

`twiddle` reads and controls ATORCH USB and DC Bluetooth LE power meters. Use
the **CLI**: it is the stable, scriptable interface. Add `--json` when you need
to parse the output.

## Command recipes

```sh
twiddle list --json                     # all meters and their latest readings
twiddle get <name> --json               # one meter's status and reading
twiddle scan --json                     # meters advertising service FFE0 nearby
twiddle reset <name> --counter all -y   # clear counters (destructive, see below)
twiddle config                          # effective config: source, socket, meters
twiddle service                         # run the service in the foreground
```

Meter names come from the config file. No meters are built in, so run
`twiddle config` to see which names exist. Never hard-code a MAC address in code
that calls this tool: use the name and let the config resolve it.

## Architecture

- A **service** keeps one Bluetooth LE connection per meter and caches the
  latest reading. The **CLI** talks to it over a Unix socket. The socket path is
  `$TWIDDLE_SOCKET` if set, else `$XDG_RUNTIME_DIR/twiddle.sock`, else
  `/tmp/twiddle.sock`. A `socket` setting in the config file overrides it, and
  `--socket` overrides both.
- A meter accepts **one connection at a time**. Do not write a Bluetooth client
  that connects to a meter while the service runs. Go through the CLI, the
  socket or the HTTP endpoint instead.
- If no service is running, `get` and `reset` fall back to a slower one-shot
  connection of their own.

## Exit codes

- `0` success, `1` runtime error (printed to stderr), `2` usage error or
  unknown meter name.

## JSON shapes

`twiddle get <name> --json` prints:

```json
{
  "name": "meter-a", "mac": "AA:BB:CC:DD:EE:01", "note": "USB load test",
  "connected": true, "connect_count": 3, "fail_streak": 0,
  "last_error": null, "age_s": 0.3,
  "reading": {
    "device_type": "USB", "voltage": 5.08, "current": 0.52, "power": 2.642,
    "capacity_ah": 1.234, "energy_wh": 6.27, "usb_dminus": 0.0,
    "usb_dplus": 0.0, "temperature_c": 27, "runtime_s": 7384,
    "checksum_ok": true, "raw": "ff5501..."
  }
}
```

`reading` is `null` until the first frame arrives. Check `connected` and
`age_s` (seconds since the last frame) for freshness. `twiddle list --json`
prints `{"ok": true, "devices": {<name>: <status>, ...}}`.

## Optional HTTP endpoint

Only when `http_port` is set in `[service]`:

```sh
curl -s http://127.0.0.1:8787/
curl -s http://127.0.0.1:8787/device/meter-a
```

## The protocol library

`twiddle.protocol` is a pure codec with no I/O, safe to import in tests and
tools:

```python
from twiddle import protocol as P
r = P.decode_report(frame_bytes)         # -> Reading | None
P.verify_checksum(frame_bytes)           # -> bool
P.build_reset("all", P.DEV_USB)          # -> bytes ff 55 11 03 05 00 00 00 00 5d
```

The meters use service `0000ffe0-…` and characteristic `0000ffe1-…` (notify and
write). Once you subscribe, a meter sends a report frame about once a second;
no start command is needed. See `docs/PROTOCOL.md`.

## Counter reset is destructive

`twiddle reset` permanently clears the meter's accumulated A·h, W·h and
runtime. **Do not run it on your own initiative.** Run it only when the human
asked to clear counters, and name the meter and counter back to them first.
Without `-y` the CLI asks for confirmation, which blocks a non-interactive
agent. That friction is intended: do not pass `-y` for them without a clear go.

## Adding or renaming meters

Edit the active config file (`twiddle config` shows its `source`):

```toml
[devices.meter-c]
mac  = "AA:BB:CC:DD:EE:03"
note = "free text"
```

Find MAC addresses with `twiddle scan --json`; meters not yet in the config
show `"name": null`. Restart the service after editing.

## Tests and development

```sh
pip install -e .            # editable install; provides the `twiddle` command
python -m pytest            # hardware-free tests
```

Modules: `protocol.py` (codec, no I/O), `client.py` (Bluetooth connections and
one-shot helpers), `service.py` (connections, socket and HTTP), `cli.py`,
`config.py`. Run the tests after any change to `protocol.py`. Brief `OFFLINE`
states and a rising `fail_streak` are expected with these meters; the service
reconnects on its own, so do not remove the retry or lock logic in `client.py`.

## Troubleshooting

- **One meter shows `OFFLINE` and `fail_streak` rises:** the service keeps
  retrying. If it persists, raise `[ble] connect_timeout` or lower
  `clear_cache_after` in the config.
- **Every meter is offline:** check the adapter (`rfkill list`), that the user
  is in the `bluetooth` group, and that no phone app holds the meters (each
  takes one connection at a time).
- **`twiddle list` says `no service`:** start `twiddle service` or the systemd
  unit. `get` and `reset` still work without it, through a one-shot connection.
