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
from dataclasses import dataclass

from . import protocol as P

log = logging.getLogger("twiddle.client")


def reset_frame(device_type: str | None, counter: str) -> bytes:
    """The reset command for a meter whose report frames say `device_type`.

    A command frame carries a device type, and it must be the meter's own, so
    it comes from the meter's last report frame with a valid checksum. With no
    such frame, or a device type the protocol does not list, nothing is sent.
    """
    if device_type is None:
        raise RuntimeError("no report frame with a valid checksum from this meter "
                           "yet, so its type is unknown; try again once it is reporting")
    dev_type = P.DEV_CODES.get(device_type)
    if dev_type is None:
        raise RuntimeError(f"meter reports unknown device type {device_type}; "
                           f"not sending a reset")
    return P.build_reset(counter, device_type=dev_type)


@dataclass
class Command:
    """A frame waiting for the writer. `started` is set just before the write
    begins, after which the frame may reach the meter whatever the caller does."""
    frame: bytes
    fut: asyncio.Future
    started: bool = False


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
        # device type from the last frame whose checksum matched; a reset
        # uses this, never the type of a frame that may be corrupt
        self.device_type: str | None = None
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
                cmd = await asyncio.wait_for(self._cmd_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            fut = cmd.fut
            if fut.done():
                continue  # the caller gave up; never write it later
            try:
                cmd.started = True
                await client.write_gatt_char(P.CHAR_UUID, cmd.frame, response=False)
                if not fut.done():
                    fut.set_result(True)
            except Exception as e:
                if not fut.done():
                    fut.set_exception(e)
        # nothing queued on this connection may be written on the next one
        self._fail_pending()

    def _fail_pending(self) -> None:
        while not self._cmd_q.empty():
            fut = self._cmd_q.get_nowait().fut
            if not fut.done():
                fut.set_exception(ConnectionError(
                    f"{self.name} disconnected before the command was sent"))

    def _on_notify(self, _char, data: bytearray) -> None:
        self._buf.extend(data)
        for frame in P.find_frames(self._buf):
            reading = P.decode_report(frame)
            if reading is not None:
                self.last_reading = reading
                self.last_frame_ts = time.time()
                if reading.checksum_ok:
                    self.device_type = reading.device_type

    async def send_command(self, frame: bytes) -> None:
        if not self.connected:
            raise RuntimeError(f"{self.name} is not connected")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        cmd = Command(frame, fut)
        await self._cmd_q.put(cmd)
        try:
            await asyncio.wait_for(fut, timeout=self.ble.command_timeout)
        except asyncio.TimeoutError:
            fut.cancel()
            t = f"{self.ble.command_timeout:g}s"
            if cmd.started:  # the write is under way and cannot be recalled
                raise TimeoutError(f"reset may still be applied: {self.name} did not "
                                   f"confirm it within {t}; read the meter before "
                                   f"you send it again") from None
            raise TimeoutError(f"reset was not sent: {self.name} did not accept it "
                               f"within {t}") from None
        except asyncio.CancelledError:
            fut.cancel()
            raise

    async def reset(self, counter: str = "all") -> None:
        await self.send_command(reset_frame(self.device_type, counter))

    def stop(self) -> None:
        self._stop = True
        self._disconnected.set()
        self._fail_pending()


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


async def _first_reading(client, settle: float, valid_only: bool = False) -> P.Reading:
    """Subscribe and wait up to `settle` seconds for a decoded report frame;
    with `valid_only`, for one whose checksum matches."""
    buf = bytearray()
    result: list[P.Reading] = []

    def cb(_c, data):
        buf.extend(data)
        for fr in P.find_frames(buf):
            r = P.decode_report(fr)
            if r and (r.checksum_ok or not valid_only):
                result.append(r)

    await client.start_notify(P.CHAR_UUID, cb)
    deadline = asyncio.get_running_loop().time() + settle
    while not result and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)
    if not result:
        what = "report frame with a valid checksum" if valid_only else "report frame"
        raise TimeoutError(f"connected but no {what} received")
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
    """Connect, read report frames until one has a valid checksum, write the
    reset for that frame's device type, disconnect. Returns the device type
    ("USB", "DC" or "AC"). For CLI use without a running service."""
    if counter.lower() not in P.RESET_CODES:
        raise ValueError(f"unknown counter {counter!r}; choose from {sorted(P.RESET_CODES)}")
    client = await _connect_retry(mac, ble, attempts)
    try:
        reading = await _first_reading(client, settle, valid_only=True)
        await client.write_gatt_char(P.CHAR_UUID,
                                     reset_frame(reading.device_type, counter),
                                     response=False)
        await asyncio.sleep(0.3)
        return reading.device_type
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
