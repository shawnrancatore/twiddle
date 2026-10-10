"""twiddle command-line tool.

Talks to a running twiddle service over its Unix socket when available;
otherwise falls back to a direct one-shot BLE connection (handy for testing,
but it competes for the meter's single BLE connection, so stop the service
first). Designed to be script/agent friendly: every subcommand supports
`--json` and uses clear exit codes (0 ok, 1 runtime error, 2 usage error).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys

from . import __version__, protocol as P
from . import config as configmod


class SocketDenied(Exception):
    """The service socket exists but this user may not open it."""


def _ipc(cfg, req: dict, timeout: float = 12.0) -> dict | None:
    """Send one request to the service; return response, or None if no service.

    A socket this user may not open most likely belongs to a running service,
    so that raises SocketDenied instead of returning None: falling back to a
    direct connection would fight that service for the meter.
    """
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(cfg.socket)
    except PermissionError:
        raise SocketDenied(
            f"permission denied on {cfg.socket}; the socket's owner and group "
            f"decide who may use it")
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        return None
    try:
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode())
    finally:
        s.close()


def _fmt_runtime(sec) -> str:
    if sec is None:
        return "?"
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def _print_reading(name: str, st: dict) -> None:
    r = st.get("reading")
    conn = "online" if st.get("connected") else "OFFLINE"
    note = f"  ({st['note']})" if st.get("note") else ""
    age = st.get("age_s")
    age_s = f"  [{age}s ago]" if age is not None else ""
    print(f"{name}  {st['mac']}  {conn}{note}{age_s}")
    if not r:
        if st.get("last_error"):
            print(f"    last_error: {st['last_error']}")
        return
    warn = "" if r.get("checksum_ok", True) else "  !! checksum mismatch"
    print(f"    {r['device_type']}  "
          f"V={r.get('voltage')}V  I={r.get('current')}A  P={r.get('power')}W{warn}")
    line2 = []
    if r.get("capacity_ah") is not None:
        line2.append(f"cap={r['capacity_ah']}Ah")
    if r.get("energy_wh") is not None:
        line2.append(f"energy={r['energy_wh']}Wh")
    if r.get("runtime_s") is not None:
        line2.append(f"runtime={_fmt_runtime(r['runtime_s'])}")
    if r.get("temperature_c") is not None:
        line2.append(f"temp={r['temperature_c']}C")
    if line2:
        print("    " + "  ".join(line2))


# -- subcommands ----------------------------------------------------------

def cmd_list(cfg, args) -> int:
    resp = _ipc(cfg, {"cmd": "list"})
    if resp is None:
        print(f"no service at {cfg.socket}; configured devices:", file=sys.stderr)
        devs = {n: {"mac": d.mac, "note": d.note, "connected": False, "reading": None}
                for n, d in cfg.devices.items()}
        resp = {"ok": True, "devices": devs}
    if args.json:
        print(json.dumps(resp, indent=2)); return 0 if resp.get("ok") else 1
    devices = resp.get("devices", {})
    if not devices:
        print("no devices configured. Run `twiddle scan` to find your meters, "
              "then add them to a config file (see config.example.toml).",
              file=sys.stderr)
        return 0
    for name, st in devices.items():
        _print_reading(name, st)
    return 0


def cmd_get(cfg, args) -> int:
    dev = cfg.device(args.device)  # validates name
    resp = _ipc(cfg, {"cmd": "get", "device": args.device})
    if resp is None:
        # fall back to direct one-shot read
        from .client import read_once
        try:
            reading = asyncio.run(read_once(dev.mac, cfg.ble))
        except Exception as e:
            print(f"error: {type(e).__name__}: {e}", file=sys.stderr); return 1
        resp = {"ok": True, "status": {"name": dev.name, "mac": dev.mac, "note": dev.note,
                                       "connected": True, "reading": reading.to_dict()}}
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(resp["status"], indent=2)); return 0
    _print_reading(dev.name, resp["status"])
    return 0


def cmd_watch(cfg, args) -> int:
    import time
    try:
        while True:
            rc = cmd_get(cfg, args)
            if rc != 0 and not args.json:
                pass
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_reset(cfg, args) -> int:
    dev = cfg.device(args.device)
    if args.counter not in P.RESET_CODES:
        print(f"error: counter must be one of {sorted(P.RESET_CODES)}", file=sys.stderr)
        return 2
    if not args.yes:
        ans = input(f"Clear '{args.counter}' counter(s) on {dev.name} ({dev.mac})? "
                    f"This is irreversible. [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("aborted"); return 1
    resp = _ipc(cfg, {"cmd": "reset", "device": args.device, "counter": args.counter,
                      "confirm": True})
    if resp is None:
        # no service: connect directly, learn the meter's type from one report
        # frame, then send the reset for that type
        from .client import reset_once
        try:
            asyncio.run(reset_once(dev.mac, args.counter, cfg.ble))
            resp = {"ok": True}
        except Exception as e:
            print(f"error: {type(e).__name__}: {e}", file=sys.stderr); return 1
    if args.json:
        print(json.dumps(resp))
    if resp.get("ok"):
        print(f"reset '{args.counter}' sent to {dev.name}")
        return 0
    print(f"error: {resp.get('error')}", file=sys.stderr)
    return 1


def cmd_scan(cfg, args) -> int:
    async def _scan():
        from bleak import BleakScanner
        found = {}

        def cb(d, adv):
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            if P.SERVICE_UUID in uuids:
                found[d.address] = adv.rssi
        async with BleakScanner(cb):
            await asyncio.sleep(args.duration)
        return found
    try:
        found = asyncio.run(_scan())
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr); return 1
    known = {d.mac: n for n, d in cfg.devices.items()}
    if args.json:
        print(json.dumps([{"mac": m, "rssi": r, "name": known.get(m)}
                          for m, r in sorted(found.items(), key=lambda x: -x[1])], indent=2))
        return 0
    print(f"ATORCH-class (FFE0) devices seen in {args.duration:.0f}s:")
    for mac, rssi in sorted(found.items(), key=lambda x: -x[1]):
        tag = f"  <- {known[mac]}" if mac in known else "  (unmapped)"
        print(f"  {mac}  rssi={rssi}{tag}")
    return 0


def cmd_service(cfg, args) -> int:
    from .service import run
    run(cfg)
    return 0


def cmd_config(cfg, args) -> int:
    info = {"source": cfg.source, "socket": cfg.socket, "http_port": cfg.http_port,
            "http_cors_origin": cfg.http_cors_origin,
            "devices": {n: {"mac": d.mac, "note": d.note} for n, d in cfg.devices.items()}}
    print(json.dumps(info, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="twiddle",
                                description="Read & control ATORCH USB/DC BLE testers.")
    p.add_argument("--config", help="path to config TOML")
    p.add_argument("--socket", help="override service socket path")
    p.add_argument("--version", action="version", version=f"twiddle {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="list devices and latest readings")
    sp.add_argument("--json", action="store_true"); sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("get", help="get a single device reading")
    sp.add_argument("device"); sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_get)

    sp = sub.add_parser("watch", help="repeatedly print a device reading")
    sp.add_argument("device"); sp.add_argument("--interval", type=float, default=1.0)
    sp.add_argument("--json", action="store_true"); sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("reset", help="clear counters on a device")
    sp.add_argument("device")
    sp.add_argument("--counter", default="all", choices=sorted(P.RESET_CODES))
    sp.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    sp.add_argument("--json", action="store_true"); sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser("scan", help="scan for ATORCH-class (FFE0) devices")
    sp.add_argument("--duration", type=float, default=8.0)
    sp.add_argument("--json", action="store_true"); sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("service", help="run the persistent service (foreground)")
    sp.set_defaults(func=cmd_service)

    sp = sub.add_parser("config", help="show effective configuration")
    sp.set_defaults(func=cmd_config)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = configmod.load(args.config)
    except ValueError as e:  # includes TOML syntax errors
        print(f"error: config: {e}", file=sys.stderr); return 2
    if args.socket:
        cfg.socket = args.socket
    try:
        return args.func(cfg, args)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr); return 2
    except SocketDenied as e:
        print(f"error: {e}", file=sys.stderr); return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
