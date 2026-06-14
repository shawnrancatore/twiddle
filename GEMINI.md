# GEMINI.md

Context for the **Gemini CLI** in this repo. The authoritative agent guide is
[`AGENTS.md`](AGENTS.md) — follow it. Gemini-specific notes only below.

`twiddle` = resilient service + CLI for ATORCH USB/DC BLE power meters. Refer to
meters by their configured **name** (default: `twdlDEE`, `twdlDUM`), never by
raw MAC.

## Most-used commands

```bash
twiddle list --json          # all meters + latest readings
twiddle get <name> --json    # one meter (parse the JSON)
twiddle scan --json          # discover FFE0 meters, with name mapping
twiddle config               # device names + active config file
python -m pytest             # hardware-free protocol tests
```

## Must-knows

- One BLE central per meter: use the running service (socket/HTTP), don't write
  a parallel direct BLE client.
- `twiddle reset` **permanently clears** the meter counters (Ah/Wh/runtime).
  Only run on explicit user request; don't auto-pass `-y`.
- Transient `OFFLINE`/retries are normal and self-healing — don't strip the
  reconnect/lock logic in `client.py`.

See `AGENTS.md` for JSON schemas, exit codes, HTTP endpoint, and config.
