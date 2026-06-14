# CLAUDE.md

Guidance for **Claude Code** working in this repo. The full agent guide is
[`AGENTS.md`](AGENTS.md) — read it; everything there applies. This file only
adds Claude-specific notes.

## What this repo is

`twiddle`: a resilient service + CLI to read/control ATORCH USB/DC BLE power
meters. Meters are referenced by **name** (configured), not MAC.

## Fast path

```bash
twiddle list --json        # current readings for all meters
twiddle get <name> --json  # one meter
twiddle config             # show device names + config source
python -m pytest           # hardware-free tests (run after editing protocol.py)
```

## Rules of thumb

- **Don't** open a second BLE connection to a meter while the service runs —
  they allow only one central. Query the running service (CLI/socket/HTTP).
- **`twiddle reset` is destructive** (wipes Ah/Wh/runtime). Treat it like a
  migration: only run it when the user explicitly asks, name the device and
  counter back to them, and don't pass `-y` on their behalf without a clear go.
- After changing `src/twiddle/protocol.py`, run the tests — the codec is
  validated against a real captured frame and the documented reset commands.
- BLE flakiness (`fail_streak`, brief `OFFLINE`) is expected; the supervisor
  self-heals. Don't "fix" it by removing the retry/lock logic.

## Layout

`src/twiddle/{protocol,client,service,cli,config}.py`, `tests/`, `docs/`,
`systemd/`, `config.example.toml`.
