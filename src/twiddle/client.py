"""Resilient BLE access to ATORCH testers via bleak.

The HM-10-style BLE module in these meters is connection-flaky: BlueZ often
reports `le-connection-abort-by-local` / connection timeouts, and the module
needs a few seconds to re-advertise after every disconnect. The vendor app
hides this with a managed BLE library that retries and keeps ONE long-lived
link. We do the same:

  * one persistent connection per device, owned by the service;
  * a supervisor loop that reconnects forever with backoff;
  * a stream watchdog that forces a reconnect if frames stop arriving;
  * optional BlueZ cache clearing (`bluetoothctl remove`) after repeated
    failures, which empirically helps the controller recover.

`bleak` is imported lazily so the rest of the package (protocol, CLI help,
tests) works without it installed.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from . import protocol as P

log = logging.getLogger("twiddle.client")


def reset_frame(reading: P.Reading | None, counter: str) -> bytes:
    """The reset command for the meter that sent `reading`.

    A command frame carries a device type, and it must be the meter's own, so
    it comes from the meter's last report frame. With no frame yet, or a
    device type the protocol does not list, nothing is sent.
    """
    if reading is None:
        raise RuntimeError("no report frame from this meter yet, so its type is "
                           "unknown; try again once it is reporting")
    dev_type = P.DEV_CODES.get(reading.device_type)
    if dev_type is None:
        raise RuntimeError(f"meter reports unknown device type {reading.device_type}; "
                           f"not sending a reset")
    return P.build_reset(counter, device_type=dev_type)


async def _clear_bluez_cache(mac: str) -> None:
    """Best-effort `bluetoothctl remove <mac>` to drop stale BlueZ state."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "remove", mac,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        pass


class DeviceManager:
    """Maintains a persistent, self-healing connection to one tester and
    caches its latest decoded :class:`~twiddle.protocol.Reading`."""

    def __init__(self, name: str, mac: str, ble, note: str = "", connect_lock=None):
        self.name = name
        self.mac = mac.upper()
        self.note = note
        self.ble = ble
        # Shared across all managers: only one device may run the
        # scan+connect critical section at a time. Concurrent active scans
        # collide in BlueZ and prevent connections. Held only during
        # (re)connect, released once streaming, so all devices stay
        # connected simultaneously afterwards.
        self._connect_lock = connect_lock or asyncio.Lock()
        self.connected = False
        self.last_reading: P.Reading | None = None
        self.last_frame_ts: float = 0.0
        self.connect_count = 0
        self.fail_streak = 0
        self.last_error: str | None = None
        self._buf = bytearray()
        self._cmd_q: asyncio.Queue = asyncio.Queue()
        self._disconnected = asyncio.Event()
        self._stop = False

    # -- status -----------------------------------------------------------
    def status(self) -> dict:
        age = (time.time() - self.last_frame_ts) if self.last_frame_ts else None
        return {
            "name": self.name,
            "mac": self.mac,
            "note": self.note,
            "connected": self.connected,
            "connect_count": self.connect_count,
            "fail_streak": self.fail_streak,
            "last_error": self.last_error,
            "age_s": round(age, 1) if age is not None else None,
            "reading": self.last_reading.to_dict() if self.last_reading else None,
        }

    # -- lifecycle --------------------------------------------------------
    async def run(self) -> None:
        """Supervisor: keep a connection alive forever."""
        from bleak import BleakClient, BleakScanner

        while not self._stop:
            try:
                if (self.ble.clear_cache_after
                        and self.fail_streak
                        and self.fail_streak % self.ble.clear_cache_after == 0):
                    log.info("%s: clearing BlueZ cache after %d failures",
                             self.name, self.fail_streak)
                    await _clear_bluez_cache(self.mac)

                # Scan + connect under the shared lock so devices don't fight
                # over the radio. The lock is released as soon as we're
                # streaming (see below), before the long-lived _serve loop.
                async with self._connect_lock:
                    dev = await BleakScanner.find_device_by_address(
                        self.mac, timeout=self.ble.connect_timeout)
                    if dev is None:
                        raise TimeoutError("not advertising")
                    self._disconnected.clear()
                    client = BleakClient(
                        dev, timeout=self.ble.connect_timeout,
                        disconnected_callback=lambda _c: self._disconnected.set())
                    await client.connect()
                    if not client.is_connected:
                        raise ConnectionError("connect returned not-connected")
                    await client.start_notify(P.CHAR_UUID, self._on_notify)
                    self.connected = True
                    self.connect_count += 1
                    self.fail_streak = 0
                    self.last_error = None
                    self.last_frame_ts = time.time()  # grace before watchdog
                    log.info("%s: connected (%s)", self.name, self.mac)
                # lock released; stream until disconnect/stale/stop
                try:
                    await self._serve(client)
                finally:
                    with contextlib.suppress(Exception):
                        await client.disconnect()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.fail_streak += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log.debug("%s: session ended: %s", self.name, self.last_error)
            finally:
                self.connected = False
            if not self._stop:
                await asyncio.sleep(self.ble.reconnect_delay)

    async def _serve(self, client) -> None:
        """Inner loop while connected: pump commands + watchdog."""
        while client.is_connected and not self._disconnected.is_set() and not self._stop:
            if time.time() - self.last_frame_ts > self.ble.stale_timeout:
                raise TimeoutError("stream went stale")
            try:
                frame, fut = await asyncio.wait_for(self._cmd_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await client.write_gatt_char(P.CHAR_UUID, frame, response=False)
                if not fut.done():
                    fut.set_result(True)
            except Exception as e:
                if not fut.done():
                    fut.set_exception(e)

    def _on_notify(self, _char, data: bytearray) -> None:
        self._buf.extend(data)
        for frame in P.find_frames(self._buf):
            reading = P.decode_report(frame)
            if reading is not None:
                self.last_reading = reading
                self.last_frame_ts = time.time()

    async def send_command(self, frame: bytes) -> None:
        if not self.connected:
            raise RuntimeError(f"{self.name} is not connected")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._cmd_q.put((frame, fut))
        await asyncio.wait_for(fut, timeout=self.ble.command_timeout)

    async def reset(self, counter: str = "all") -> None:
        await self.send_command(reset_frame(self.last_reading, counter))

    def stop(self) -> None:
        self._stop = True
        self._disconnected.set()


# -- one-shot helpers (used by the CLI when no service is running) --------

async def _connect_retry(mac: str, ble, attempts: int):
    from bleak import BleakClient, BleakScanner
    last = None
    for _ in range(attempts):
        try:
            dev = await BleakScanner.find_device_by_address(mac, timeout=ble.connect_timeout)
            if dev is None:
                last = TimeoutError("not advertising")
                continue
            client = BleakClient(dev, timeout=ble.connect_timeout)
            await client.connect()
            if client.is_connected:
                return client
            last = ConnectionError("not-connected")
        except Exception as e:
            last = e
            await asyncio.sleep(ble.reconnect_delay)
    raise last or TimeoutError("connect failed")


async def _first_reading(client, settle: float) -> P.Reading:
    """Subscribe and wait up to `settle` seconds for a decoded report frame."""
    buf = bytearray()
    result: list[P.Reading] = []

    def cb(_c, data):
        buf.extend(data)
        for fr in P.find_frames(buf):
            r = P.decode_report(fr)
            if r:
                result.append(r)

    await client.start_notify(P.CHAR_UUID, cb)
    deadline = asyncio.get_running_loop().time() + settle
    while not result and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)
    if not result:
        raise TimeoutError("connected but no report frame received")
    return result[-1]


async def read_once(mac: str, ble, attempts: int = 20, settle: float = 6.0) -> P.Reading:
    """Connect, grab one decoded reading, disconnect. For CLI use without a
    running service (competes for the single-central connection)."""
    client = await _connect_retry(mac, ble, attempts)
    try:
        return await _first_reading(client, settle)
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


async def reset_once(mac: str, counter: str, ble, attempts: int = 20,
                     settle: float = 6.0) -> str:
    """Connect, read one report frame to learn the meter's device type, write
    the reset for that type, disconnect. Returns the device type ("USB", "DC"
    or "AC"). For CLI use without a running service."""
    if counter.lower() not in P.RESET_CODES:
        raise ValueError(f"unknown counter {counter!r}; choose from {sorted(P.RESET_CODES)}")
    client = await _connect_retry(mac, ble, attempts)
    try:
        reading = await _first_reading(client, settle)
        await client.write_gatt_char(P.CHAR_UUID, reset_frame(reading, counter),
                                     response=False)
        await asyncio.sleep(0.3)
        return reading.device_type
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
