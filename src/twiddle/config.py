"""Configuration loading for twiddle.

Config is TOML (read with the stdlib `tomllib`, Python >=3.11). Search order:
  1. $TWIDDLE_CONFIG
  2. ./twiddle.toml
  3. ~/.config/twiddle/config.toml
  4. /etc/twiddle/config.toml
If no file is found, built-in defaults are used (with the two bench devices).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SOCKET = os.environ.get(
    "TWIDDLE_SOCKET",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "twiddle.sock"),
)

# Bench defaults. The two ATORCH testers are interchangeable twins, so the
# DEE/DUM name<->MAC mapping below is a starting guess — swap freely in config.
DEFAULT_DEVICES = {
    "twdlDEE": {"mac": "78:5F:2D:C4:0C:BD", "note": "servo / PWM power bus"},
    "twdlDUM": {"mac": "D9:9B:58:F4:F1:0B", "note": "logic / Pi rail"},
}


@dataclass
class BleTuning:
    connect_timeout: float = 8.0     # per connect attempt (incl. scan)
    reconnect_delay: float = 2.0     # wait between reconnect attempts
    stale_timeout: float = 12.0      # reconnect if no frame for this long
    command_timeout: float = 8.0     # wait for a command write to complete
    clear_cache_after: int = 6       # bluetoothctl remove after N failures (0=never)


@dataclass
class Device:
    name: str
    mac: str
    note: str = ""


@dataclass
class Config:
    socket: str = DEFAULT_SOCKET
    http_port: int | None = None     # optional read-only HTTP/JSON endpoint
    log_level: str = "INFO"
    ble: BleTuning = field(default_factory=BleTuning)
    devices: dict[str, Device] = field(default_factory=dict)
    source: str = "<defaults>"

    def device(self, name: str) -> Device:
        try:
            return self.devices[name]
        except KeyError:
            raise KeyError(
                f"unknown device {name!r}; known: {', '.join(self.devices) or '(none)'}"
            )


_SEARCH = [
    os.environ.get("TWIDDLE_CONFIG"),
    "twiddle.toml",
    os.path.expanduser("~/.config/twiddle/config.toml"),
    "/etc/twiddle/config.toml",
]


def find_config_path(explicit: str | None = None) -> str | None:
    for p in [explicit, *_SEARCH]:
        if p and Path(p).is_file():
            return p
    return None


def load(explicit: str | None = None) -> Config:
    path = find_config_path(explicit)
    raw: dict = {}
    if path:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)

    cfg = Config()
    cfg.source = path or "<defaults>"

    svc = raw.get("service", {})
    cfg.socket = svc.get("socket", cfg.socket)
    cfg.http_port = svc.get("http_port", cfg.http_port)
    cfg.log_level = svc.get("log_level", cfg.log_level)

    ble = raw.get("ble", {})
    for f_ in BleTuning().__dataclass_fields__:
        if f_ in ble:
            setattr(cfg.ble, f_, ble[f_])

    devices = raw.get("devices") or DEFAULT_DEVICES
    for name, spec in devices.items():
        cfg.devices[name] = Device(name=name, mac=str(spec["mac"]).upper(),
                                   note=spec.get("note", ""))
    return cfg
