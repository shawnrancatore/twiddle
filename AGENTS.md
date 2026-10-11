# AGENTS.md

A guide for coding agents that use or change `twiddle`.

`twiddle` reads and controls ATORCH USB and DC Bluetooth LE power meters. Use
the **CLI**: it is the stable, scriptable interface. Add `--json` when you need
to parse the output.

## Command recipes

```sh
twiddle list --json                     # all meters and their latest readings
twiddle get <name> --json               # one meter's status and reading
twiddle scan --json                     # devices advertising service FFE0 nearby (any device, with its name; not only meters)
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
  connection of their own. If the socket exists but this user may not open it,
  the CLI stops with `permission denied` and does not connect directly.
- The socket speaks one JSON object per line. A `reset` request must carry
  `"confirm": true`, or the service refuses it; the CLI adds it after its
  prompt or with `-y`. The socket is mode 0660, so its owner and group decide
  who may read meters and reset counters.

## Exit codes

- `0` success, `1` runtime error (printed to stderr), `2` usage error or
  unknown meter name.

## JSON shapes

With the service running, `twiddle get <name> --json` prints:

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
`age_s` for freshness: `age_s` is the time since the last frame was received
(not since the connect), and `null` until the first frame of the service
process. After a reconnect an old reading keeps its growing age until a new
frame arrives.

With no service running, `twiddle get <name> --json` makes a one-shot
connection and prints only `name`, `mac`, `note`, `connected` (always `true`)
and `reading`. It has no `connect_count`, `fail_streak`, `last_error` or
`age_s`, and the text form shows no age. `twiddle list --json` with no service
prints each configured device with `mac`, `note`, `connected: false` and
`reading: null` only. `twiddle list --json`
prints `{"ok": true, "devices": {<name>: <status>, ...}}`.

## Optional HTTP endpoint

Only when `http_port` is set in `[service]`. It listens on 127.0.0.1, answers
`GET` only, and cannot reset anything:

```sh
curl -s http://127.0.0.1:8731/
curl -s http://127.0.0.1:8731/device/meter-a
```

Errors carry real status codes: `404` for an unknown meter or path, `405` for
any method but `GET`, `403` for a `Host` other than `127.0.0.1` or
`localhost`, `400` for a malformed request. It sends no CORS header unless
`http_cors_origin` names one origin.

## The protocol library

`twiddle.protocol` is a pure codec with no I/O, so tests and tools can import
it without a meter:

```python
from twiddle import protocol as P
r = P.decode_report(frame_bytes)             # -> Reading | None
P.verify_checksum(frame_bytes)               # -> bool
P.build_reset("all", device_type=P.DEV_USB)  # -> bytes ff 55 11 03 05 00 00 00 00 5d
```

`device_type` has no default: pass the meter's own type, from its report
frames.

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

Find MAC addresses with `twiddle scan --json`. It lists every device
advertising FFE0, not only meters: each entry has `advertised_name` (`null`
when the device sent none) to tell them apart, and `name` (`null` for a device
not yet in the config). ATORCH meters advertise names such as `UD18_BLE`. Restart the service after editing.

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
  `clear_cache_after` in the config. `clear_cache_after` runs
  `bluetoothctl remove <mac>`, which also deletes any pairing or trust stored
  for that address.
- **A meter drops soon after connecting:** seen in 2 of 12 service starts; it
  reconnects by itself. After a forced drop, reconnecting took 6.6 s and 8.5 s.
  Wait for a new frame rather than trusting `connected` alone.
- **Every meter is offline:** check the adapter (`rfkill list`), that the user
  is in the `bluetooth` group, and that no phone app holds the meters (each
  takes one connection at a time).
- **`twiddle list` says `no service`:** start `twiddle service` or the systemd
  unit. `get` and `reset` still work without it, through a one-shot connection.
