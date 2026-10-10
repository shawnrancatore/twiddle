"""Configuration loading for twiddle.

Config is TOML (read with the stdlib `tomllib`, Python >=3.11). Search order:
  1. $TWIDDLE_CONFIG
  2. ./twiddle.toml
  3. ~/.config/twiddle/config.toml
  4. /etc/twiddle/config.toml
If no file is found, built-in defaults are used and no devices are configured
(run `twiddle scan`, then add your meters to a config — see config.example.toml).
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SOCKET = os.environ.get(
    "TWIDDLE_SOCKET",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "twiddle.sock"),
)

# No devices are baked in: a fresh install has nothing to connect to until you
# add your own meters to a config file. See config.example.toml for the format
# and `twiddle scan` to discover MACs.
DEFAULT_DEVICES: dict[str, dict[str, str]] = {}


@dataclass
class BleTuning:
    connect_timeout: float = 8.0     # seconds for the scan, and again for the connection
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
    http_cors_origin: str | None = None  # one web origin allowed to read it
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


def _origin(value) -> str | None:
    """Validate [service] http_cors_origin: absent, or a single origin.

    "*" would let every site read the endpoint, and "null" every sandboxed
    page or local file, so neither is accepted.
    """
    if value is None:
        return None
    if (not isinstance(value, str) or not re.fullmatch(r"[!-~]+", value)
            or value == "*" or value.lower() == "null"):
        raise ValueError('[service] http_cors_origin must be one origin, '
                         'such as "http://localhost:3000", not "*" or "null"')
    return value


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
    cfg.http_cors_origin = _origin(svc.get("http_cors_origin"))
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
