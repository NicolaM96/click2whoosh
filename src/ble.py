"""Bluetooth connection to the Zwift Click v2 and decoding of the buttons.

Protocol derived from the public parts of OpenBikeControl/bikecontrol:
- the Click v2 advertises service 0xFC82 and manufacturer data 0x094A (Zwift),
  with the first byte telling the side: 0x0A right, 0x0B left
- handshake: "RideOn" is written to the sync RX characteristic
- every message starts with an opcode; 0x23 = button state (protobuf
  RideKeyPadStatus, field 1 = bitmask, bit at 0 = button pressed)
- the left Click also forwards the right one's buttons and battery: each side keeps only its own
  buttons, and the battery is read from each Click's standard characteristic 0x2A19
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakBluetoothNotAvailableError, BleakBluetoothNotAvailableReason

log = logging.getLogger("click2whoosh")
# wake_sequence() re-enables notifications directly on CoreBluetooth: bleak does not expect it
# and reports it on every cycle. It is harmless, so that message is hidden.
logging.getLogger("bleak.backends.corebluetooth.PeripheralDelegate").addFilter(
    lambda record: "didUpdateNotificationStateForCharacteristic" not in record.getMessage())

ZWIFT_MANUFACTURER_ID = 0x094A
RIDE_SERVICE_UUID = "0000fc82-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
FIRMWARE_UUID = "00002a26-0000-1000-8000-00805f9b34fb"
# The firmware everything was tested with: an update may change the protocol.
TESTED_FIRMWARE = "1.1.0"
# Characteristics used to keep the Click awake without restarting it. Sequence observed by
# capturing with PacketLogger the traffic between BikeControl and the Click (firmware 1.1.0):
# every 55 s, on the same connection, notifications re-enabled on 0100 and 0102, then
# 0102 <- 01 (reply 01 f0 00), 0100 <- WAKE_MESSAGE (reply 00), 0102 <- 03 (reply 12 00 ...).
WAKE_CHAR_UUID = "00000100-19ca-4651-86e5-fa29dcdd09d1"
WAKE_CONTROL_UUID = "00000102-19ca-4651-86e5-fa29dcdd09d1"
WAKE_MESSAGE = bytes.fromhex("3d b8 f3 96 00 00 00 00 00 01 00 00 00 b0 03 00 00 00")
ASYNC_CHAR_UUID = "00000002-19ca-4651-86e5-fa29dcdd09d1"
SYNC_RX_CHAR_UUID = "00000003-19ca-4651-86e5-fa29dcdd09d1"
SYNC_TX_CHAR_UUID = "00000004-19ca-4651-86e5-fa29dcdd09d1"

RIDE_ON = b"RideOn"
UNLOCKED_HELLO = bytes([0xFF, 0x04, 0x00])  # greeting BikeControl sends to the left Click after RideOn

OPCODE_RESET = 0x18
OPCODE_CONTROLLER = 0x23

SIDE_RIGHT = 0x0A
SIDE_LEFT = 0x0B
SIDE_NAMES = {SIDE_RIGHT: "right", SIDE_LEFT: "left"}  # identifiers; the app shows translated names

# What a Click is doing: codes, not text. The app turns them into words in the user's language.
STATUS_SEARCHING = "searching"
STATUS_RESTARTING = "restarting"
STATUS_CONNECTED = "connected"
STATUS_DISCONNECTED = "disconnected"
STATUS_UNUSED = "unused"  # Bluetooth is not used at all (tests)
STATUS_BLUETOOTH_OFF = "bluetooth_off"
STATUS_BLUETOOTH_DENIED = "bluetooth_denied"
STATUS_BLUETOOTH_UNAVAILABLE = "bluetooth_unavailable"

# The buttons: name (as in config.toml) -> (how it is called in the menu and the window, bit in the bitmask).
BUTTONS = {
    "plus": ("+", 0x01000),  # right Click
    "y": ("Y", 0x00040),
    "z": ("Z", 0x00080),
    "a": ("A", 0x00010),
    "b": ("B", 0x00020),
    "minus": ("−", 0x00100),  # left Click
    "nav_up": ("↑", 0x00002),
    "nav_down": ("↓", 0x00008),
    "nav_left": ("←", 0x00001),
    "nav_right": ("→", 0x00004),
}
# The buttons of each Click, in the order the windows list them.
SIDE_BUTTONS = {SIDE_RIGHT: ("plus", "y", "z", "a", "b"),
                SIDE_LEFT: ("minus", "nav_up", "nav_down", "nav_left", "nav_right")}
BUTTON_LABELS = {name: label for name, (label, _) in BUTTONS.items()}
BUTTON_MASKS = {name: mask for name, (_, mask) in BUTTONS.items()}
ALL_RELEASED = 0xFFFFFFFF
# Fallback if a firmware stopped accepting the "wake" sequence: restart before the minute is up.
RESET_AFTER_S = 55


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def parse_protobuf(data: bytes) -> dict[int, int]:
    """Extracts only the top-level varint fields of a protobuf message."""
    fields: dict[int, int] = {}
    pos = 0
    while pos < len(data):
        key, pos = read_varint(data, pos)
        number, wire_type = key >> 3, key & 0x07
        if wire_type == 0:
            fields[number], pos = read_varint(data, pos)
        elif wire_type == 2:
            length, pos = read_varint(data, pos)
            pos += length
        elif wire_type == 5:
            pos += 4
        elif wire_type == 1:
            pos += 8
        else:
            raise ValueError(f"unhandled wire type {wire_type}")
    return fields


def pressed_buttons(payload: bytes) -> set[str]:
    """Buttons pressed in a RideKeyPadStatus message (without the opcode)."""
    button_map = parse_protobuf(payload).get(1, ALL_RELEASED)
    return {name for name, mask in BUTTON_MASKS.items() if not button_map & mask}


def click_side(adv: AdvertisementData) -> int | None:
    data = adv.manufacturer_data.get(ZWIFT_MANUFACTURER_ID)
    if data and data[0] in SIDE_NAMES:
        return data[0]
    return None


@dataclass
class ClickEvents:
    """Callbacks called by the connection. All optional."""

    on_press: Callable[[str], None] = lambda name: None
    on_release: Callable[[str], None] = lambda name: None
    on_battery: Callable[[str, int], None] = lambda side, level: None
    on_firmware: Callable[[str, str], None] = lambda side, version: None
    on_status: Callable[[str, str], None] = lambda side, status: None
    # If False we don't keep it awake: it switches itself off (or we make it), saving the battery.
    keep_awake_allowed: Callable[[], bool] = lambda: True
    # If False (e.g. MyWhoosh closed) the Click is only searched for now and then: the Mac uses less,
    # and when a button is pressed the Click connects anyway after a few seconds.
    search_often: Callable[[], bool] = lambda: True


BLUETOOTH_PROBLEMS = {
    BleakBluetoothNotAvailableReason.POWERED_OFF: STATUS_BLUETOOTH_OFF,
    BleakBluetoothNotAvailableReason.DENIED_BY_USER: STATUS_BLUETOOTH_DENIED,
    BleakBluetoothNotAvailableReason.DENIED_BY_SYSTEM: STATUS_BLUETOOTH_DENIED,
    BleakBluetoothNotAvailableReason.DENIED_BY_UNKNOWN: STATUS_BLUETOOTH_DENIED,
}


@dataclass
class ClickSide:
    """One Click v2 (right or left): searches for it, connects and reconnects by itself."""

    side: int
    events: ClickEvents
    # "wake": sequence that keeps it on for ~15 minutes. "reset": restart before the minute is up (fallback).
    keepalive: str | None = None
    keepalive_interval: float = 300.0  # seconds between one renewal and the next
    pressed: set[str] = field(default_factory=set)
    last_message: float = 0.0
    device: BLEDevice | None = None
    just_reset: bool = False
    firmware: str = ""  # read on connection, for the log
    client: BleakClient | None = None  # the current connection, for power_off()
    # After power_off() the restarted Click shows up for a few seconds: we don't search for it
    # until it has really fallen asleep, otherwise we would reconnect for nothing.
    rest_until: float = 0.0

    @property
    def name(self) -> str:
        return SIDE_NAMES[self.side]

    async def find(self, timeout: float = 10.0) -> BLEDevice | None:
        found: asyncio.Future[BLEDevice] = asyncio.get_running_loop().create_future()

        def on_detect(device: BLEDevice, adv: AdvertisementData) -> None:
            if click_side(adv) == self.side and not found.done():
                found.set_result(device)

        async with BleakScanner(on_detect):
            try:
                return await asyncio.wait_for(found, timeout)
            except asyncio.TimeoutError:
                return None

    def handle(self, data: bytes) -> None:
        self.last_message = time.monotonic()
        if not data:
            return
        if data.startswith(RIDE_ON):
            log.info("[%s] handshake ok", self.name)
            return

        if data[0] != OPCODE_CONTROLLER:
            return
        try:
            # Only this Click's buttons: the other one's already arrive from its own connection.
            self.update_buttons(pressed_buttons(data[1:]) & set(SIDE_BUTTONS[self.side]))
        except (IndexError, ValueError) as e:
            log.warning("[%s] message not decoded %s: %s", self.name, data.hex(" "), e)

    def update_buttons(self, now: set[str]) -> None:
        for name in sorted(now - self.pressed):
            self.events.on_press(name)
        for name in sorted(self.pressed - now):
            self.events.on_release(name)
        self.pressed = now

    async def keep_awake(self, client: BleakClient) -> None:
        """Keeps the Click from switching off after 60 s, for as long as needed (see ClickEvents.keep_awake_allowed).

        "wake": the wake_sequence() sequence keeps it on for ~15 minutes and is renewed
        every keepalive_interval seconds, without disconnecting it.
        "reset": fallback for a firmware that did not accept the sequence. It is restarted before
        the minute is up (3-4 s during which buttons do not arrive); after a restart buttons do not
        postpone the switch-off, so it is done within keepalive_interval seconds of
        connecting, choosing if possible a moment with no buttons pressed.
        """
        if self.keepalive == "wake":
            wake = client.services.get_characteristic(WAKE_CHAR_UUID)
            control = client.services.get_characteristic(WAKE_CONTROL_UUID)
            if wake is None or control is None:
                log.warning("[%s] wake characteristics not found: using the reset instead", self.name)
                self.keepalive, self.keepalive_interval = "reset", RESET_AFTER_S
                return await self.keep_awake(client)
            # Just connected: if there is no need to keep it awake now, we still wait until
            # just before the minute is up, because a pressed button (e.g. for music) may change things.
            waited = 0.0
            while not self.events.keep_awake_allowed():
                if waited == 0:
                    log.info("[%s] no need to keep it awake for now", self.name)
                if waited >= 50:
                    log.info("[%s] no button pressed: it will switch itself off", self.name)
                    return
                await asyncio.sleep(2)
                waited += 2
            while True:
                await self.wake_sequence(client, wake, control)
                # Between one cycle and the next we check often whether it is still needed (e.g. MyWhoosh closed).
                waited = 0.0
                while waited < self.keepalive_interval:
                    await asyncio.sleep(5)
                    waited += 5
                    if not self.events.keep_awake_allowed():
                        # Each cycle keeps it on for ~15 minutes: we reset it to make it switch off right away.
                        await self.power_off("no need to keep it awake")
                        return

        if self.keepalive == "reset":
            connected_at = time.monotonic()
            # Each restart uses battery: we do it as late as possible, anticipating it by at most
            # quiet_window seconds so it doesn't happen while buttons are being pressed.
            quiet_window = 6
            while True:
                await asyncio.sleep(0.5)
                now = time.monotonic()
                age, idle = now - connected_at, now - self.last_message
                if age >= self.keepalive_interval:
                    break
                if age >= self.keepalive_interval - quiet_window and idle >= 2 and not self.pressed:
                    break
            if not self.events.keep_awake_allowed():
                await self.power_off("no need to keep it awake")
                return
            log.info("[%s] connected for %.0f s, buttons idle for %.0f s: restarting the Click", self.name, age, idle)
            self.just_reset = True
            await client.write_gatt_char(SYNC_RX_CHAR_UUID, bytes([OPCODE_RESET]), response=False)

    async def read_battery(self, client: BleakClient) -> None:
        try:
            self.events.on_battery(self.name, (await client.read_gatt_char(BATTERY_LEVEL_UUID))[0])
        except Exception as e:
            log.info("[%s] battery not readable: %s", self.name, e)

    async def read_firmware(self, client: BleakClient) -> None:
        try:
            version = bytes(await client.read_gatt_char(FIRMWARE_UUID)).decode("utf-8", "replace").strip()
        except Exception as e:
            log.info("[%s] firmware not readable: %s", self.name, e)
            return
        self.firmware = version
        log.info("[%s] firmware %s", self.name, version)
        self.events.on_firmware(self.name, version)

    async def wake_sequence(self, client: BleakClient, wake, control) -> None:
        """The sequence that keeps the Click on for another ~15 minutes (944 s)."""
        step = "re-enabling notifications"
        try:
            for characteristic in (wake, control):
                # BikeControl re-enables notifications on every cycle; bleak doesn't allow it if they are
                # already active, so we ask CoreBluetooth directly.
                delegate = getattr(getattr(client, "_backend", None), "_delegate", None)
                if delegate is not None:
                    delegate.peripheral.setNotifyValue_forCharacteristic_(True, characteristic.obj)
            await asyncio.sleep(0.1)
            step = "writing 01"
            await client.write_gatt_char(control, b"\x01", response=False)
            await asyncio.sleep(0.06)
            step = "writing the wake message"
            await client.write_gatt_char(wake, WAKE_MESSAGE, response=False)
            await asyncio.sleep(0.06)
            step = "writing 03"
            await client.write_gatt_char(control, b"\x03", response=False)
        except Exception as e:
            # The firmware (if known) helps tell whether a new version changed the sequence.
            log.warning("[%s] wake sequence failed at step '%s' (firmware %s): %s",
                        self.name, step, self.firmware or "?", e)
            raise
        log.info("[%s] wake sequence ok: kept awake without a restart", self.name)
        await self.read_battery(client)

    async def power_off(self, reason: str) -> None:
        """Makes the Click switch off as soon as possible. It turns back on when a button is pressed.

        The Zwift protocol's POWER_OFF command answers NOT_SUPPORTED here, so RESET is used:
        it zeroes the ~15 minutes granted by the last wake_sequence, and without a new cycle
        the Click switches itself off after about a minute.
        """
        client = self.client
        if client is None or not client.is_connected:
            return
        log.info("[%s] %s: switching it off", self.name, reason)
        self.rest_until = time.monotonic() + 70
        try:
            await client.write_gatt_char(SYNC_RX_CHAR_UUID, bytes([OPCODE_RESET]), response=False)
        except Exception as e:
            log.info("[%s] switching off failed: %s", self.name, e)

    async def run_once(self) -> bool:
        """One search and, if found, one connection until disconnection. True if it connected."""
        if self.just_reset and self.device:
            # After our own RESET we reconnect directly, without searching:
            # macOS waits for the Click to show up again. If that fails, the
            # next time we search normally.
            self.just_reset = False
            device = self.device
            self.events.on_status(self.name, STATUS_RESTARTING)
        else:
            self.events.on_status(self.name, STATUS_SEARCHING)
            often = self.events.search_often()
            device = await self.find(timeout=10 if often else 8)
            if device is None:
                if not often:
                    await asyncio.sleep(4)  # light search: 8 s of searching, 4 of pause
                return False
            self.device = device

        try:
            await self.connect_and_listen(device)
            return True
        finally:
            # Whatever the reason for closing: no buttons left "pressed" (e.g. steering).
            self.client = None
            self.update_buttons(set())

    async def connect_and_listen(self, device: BLEDevice) -> None:
        disconnected = asyncio.Event()
        loop = asyncio.get_running_loop()
        client = BleakClient(device, disconnected_callback=lambda _: loop.call_soon_threadsafe(disconnected.set))
        log.info("[%s] connecting to %s", self.name, device.name or device.address)
        async with client:
            self.client = client
            await client.start_notify(ASYNC_CHAR_UUID, lambda _, d: self.handle(bytes(d)))
            await client.start_notify(SYNC_TX_CHAR_UUID, lambda _, d: self.handle(bytes(d)))
            if self.keepalive == "wake":
                for uuid in (WAKE_CHAR_UUID, WAKE_CONTROL_UUID):
                    characteristic = client.services.get_characteristic(uuid)
                    if characteristic is not None:
                        await client.start_notify(characteristic, lambda c, d: log.debug(
                            "[%s] reply %s: %s", self.name, c.uuid[:8], bytes(d).hex(" ")))
            await client.write_gatt_char(SYNC_RX_CHAR_UUID, RIDE_ON, response=False)
            if self.side == SIDE_LEFT:
                await client.write_gatt_char(SYNC_RX_CHAR_UUID, UNLOCKED_HELLO, response=False)
            self.last_message = time.monotonic()
            self.events.on_status(self.name, STATUS_CONNECTED)
            # The battery is read on connection and on every keep-awake cycle
            # (see wake_sequence), plus the updates the Click sends by itself.
            await self.read_battery(client)
            await self.read_firmware(client)
            try:
                await client.start_notify(BATTERY_LEVEL_UUID,
                                          lambda _, d: d and self.events.on_battery(self.name, d[0]))
            except Exception as e:
                log.info("[%s] battery notifications not available: %s", self.name, e)
            keepalive = asyncio.create_task(self.keep_awake(client)) if self.keepalive else None
            if keepalive:
                keepalive.add_done_callback(self._log_keepalive_error)
            try:
                await disconnected.wait()
            finally:
                if keepalive:
                    keepalive.cancel()
        log.info("[%s] disconnected", self.name)

    def _log_keepalive_error(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.warning("[%s] error while keeping it awake: %s", self.name, task.exception())

    async def run_forever(self) -> None:
        while True:
            if time.monotonic() < self.rest_until:
                await asyncio.sleep(2)
                continue
            connected = False
            try:
                connected = await self.run_once()
            except asyncio.CancelledError:
                raise
            except BleakBluetoothNotAvailableError as e:
                self.just_reset = False
                problem = BLUETOOTH_PROBLEMS.get(e.reason, STATUS_BLUETOOTH_UNAVAILABLE)
                log.warning("[%s] %s", self.name, problem)
                self.events.on_status(self.name, problem)
                await asyncio.sleep(5)
                continue
            except Exception as e:  # BLE on macOS fails in many different ways
                log.warning("[%s] error: %s", self.name, e)
            if connected and not self.just_reset:
                self.events.on_status(self.name, STATUS_DISCONNECTED)
            await asyncio.sleep(0.2 if self.just_reset else 1)

