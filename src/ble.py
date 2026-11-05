"""Connessione Bluetooth allo Zwift Click v2 e decodifica dei tasti.

Protocollo ricavato dalle parti pubbliche di OpenBikeControl/bikecontrol:
- il Click v2 annuncia il servizio 0xFC82 e i dati produttore 0x094A (Zwift),
  col primo byte che dice il lato: 0x0A destro, 0x0B sinistro
- handshake: si scrive "RideOn" sulla caratteristica sync RX
- ogni messaggio inizia con un opcode; 0x23 = stato dei tasti (protobuf
  RideKeyPadStatus, campo 1 = bitmask, bit a 0 = tasto premuto)
- il sinistro inoltra anche tasti e batteria del destro: ogni lato tiene solo i suoi tasti,
  e la batteria si legge dalla caratteristica standard 0x2A19 di ciascun Click
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
# wake_sequence() riattiva le notifiche direttamente su CoreBluetooth: bleak non se lo aspetta
# e lo segnala a ogni ciclo. È innocuo, quindi quel messaggio si nasconde.
logging.getLogger("bleak.backends.corebluetooth.PeripheralDelegate").addFilter(
    lambda record: "didUpdateNotificationStateForCharacteristic" not in record.getMessage())

ZWIFT_MANUFACTURER_ID = 0x094A
RIDE_SERVICE_UUID = "0000fc82-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
FIRMWARE_UUID = "00002a26-0000-1000-8000-00805f9b34fb"
# Il firmware con cui è stato provato tutto: un aggiornamento può cambiare il protocollo.
TESTED_FIRMWARE = "1.1.0"
# Caratteristiche usate per tenere sveglio il Click senza riavviarlo. Sequenza osservata
# catturando con PacketLogger il traffico tra BikeControl e il Click (firmware 1.1.0):
# ogni 55 s, sulla stessa connessione, notifiche riattivate su 0100 e 0102, poi
# 0102 <- 01 (risposta 01 f0 00), 0100 <- WAKE_MESSAGE (risposta 00), 0102 <- 03 (risposta 12 00 ...).
WAKE_CHAR_UUID = "00000100-19ca-4651-86e5-fa29dcdd09d1"
WAKE_CONTROL_UUID = "00000102-19ca-4651-86e5-fa29dcdd09d1"
WAKE_MESSAGE = bytes.fromhex("3d b8 f3 96 00 00 00 00 00 01 00 00 00 b0 03 00 00 00")
ASYNC_CHAR_UUID = "00000002-19ca-4651-86e5-fa29dcdd09d1"
SYNC_RX_CHAR_UUID = "00000003-19ca-4651-86e5-fa29dcdd09d1"
SYNC_TX_CHAR_UUID = "00000004-19ca-4651-86e5-fa29dcdd09d1"

RIDE_ON = b"RideOn"
UNLOCKED_HELLO = bytes([0xFF, 0x04, 0x00])  # saluto che BikeControl manda al sinistro dopo RideOn

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

# I tasti: nome (come in config.toml) -> (come si chiama nel menu e nella finestra, bit nella bitmask).
BUTTONS = {
    "plus": ("+", 0x01000),  # Click destro
    "y": ("Y", 0x00040),
    "z": ("Z", 0x00080),
    "a": ("A", 0x00010),
    "b": ("B", 0x00020),
    "minus": ("−", 0x00100),  # Click sinistro
    "nav_up": ("↑", 0x00002),
    "nav_down": ("↓", 0x00008),
    "nav_left": ("←", 0x00001),
    "nav_right": ("→", 0x00004),
}
# I tasti di ciascun Click, nell'ordine in cui li elencano le finestre.
SIDE_BUTTONS = {SIDE_RIGHT: ("plus", "y", "z", "a", "b"),
                SIDE_LEFT: ("minus", "nav_up", "nav_down", "nav_left", "nav_right")}
BUTTON_LABELS = {name: label for name, (label, _) in BUTTONS.items()}
BUTTON_MASKS = {name: mask for name, (_, mask) in BUTTONS.items()}
ALL_RELEASED = 0xFFFFFFFF
# Riserva se un firmware non accettasse più la sequenza "wake": riavvio prima del minuto.
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
    """Estrae i soli campi varint di primo livello di un messaggio protobuf."""
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
    """Tasti premuti in un messaggio RideKeyPadStatus (senza l'opcode)."""
    button_map = parse_protobuf(payload).get(1, ALL_RELEASED)
    return {name for name, mask in BUTTON_MASKS.items() if not button_map & mask}


def click_side(adv: AdvertisementData) -> int | None:
    data = adv.manufacturer_data.get(ZWIFT_MANUFACTURER_ID)
    if data and data[0] in SIDE_NAMES:
        return data[0]
    return None


@dataclass
class ClickEvents:
    """Callback chiamate dalla connessione. Tutte opzionali."""

    on_press: Callable[[str], None] = lambda name: None
    on_release: Callable[[str], None] = lambda name: None
    on_battery: Callable[[str, int], None] = lambda side, level: None
    on_firmware: Callable[[str, str], None] = lambda side, version: None
    on_status: Callable[[str, str], None] = lambda side, status: None
    # Se False non lo si tiene sveglio: si spegne da solo (o lo si fa spegnere), risparmiando la pila.
    keep_awake_allowed: Callable[[], bool] = lambda: True
    # Se False (es. MyWhoosh chiuso) il Click si cerca solo ogni tanto: il Mac consuma meno,
    # e premendo un tasto il Click si collega comunque dopo qualche secondo.
    search_often: Callable[[], bool] = lambda: True


BLUETOOTH_PROBLEMS = {
    BleakBluetoothNotAvailableReason.POWERED_OFF: STATUS_BLUETOOTH_OFF,
    BleakBluetoothNotAvailableReason.DENIED_BY_USER: STATUS_BLUETOOTH_DENIED,
    BleakBluetoothNotAvailableReason.DENIED_BY_SYSTEM: STATUS_BLUETOOTH_DENIED,
    BleakBluetoothNotAvailableReason.DENIED_BY_UNKNOWN: STATUS_BLUETOOTH_DENIED,
}


@dataclass
class ClickSide:
    """Un Click v2 (destro o sinistro): lo cerca, si collega e si ricollega da solo."""

    side: int
    events: ClickEvents
    # "wake": sequenza che lo tiene acceso ~15 minuti. "reset": riavvio prima del minuto (riserva).
    keepalive: str | None = None
    keepalive_interval: float = 300.0  # secondi tra un rinnovo e l'altro
    pressed: set[str] = field(default_factory=set)
    last_message: float = 0.0
    device: BLEDevice | None = None
    just_reset: bool = False
    firmware: str = ""  # letto al collegamento, per il registro
    client: BleakClient | None = None  # la connessione in corso, per power_off()
    # Dopo power_off() il Click riavviato si fa vedere per qualche secondo: non lo si cerca
    # finché non si è addormentato davvero, altrimenti lo si ricollegherebbe per niente.
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
            # Solo i tasti di questo Click: quelli dell'altro arrivano già dalla sua connessione.
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
        """Evita che il Click si spenga dopo 60 s, finché serve (vedi ClickEvents.keep_awake_allowed).

        "wake": la sequenza di wake_sequence() lo tiene acceso ~15 minuti e la si rinnova
        ogni keepalive_interval secondi, senza disconnetterlo.
        "reset": riserva per un firmware che non accettasse la sequenza. Lo si riavvia prima
        che scada il minuto (3-4 s in cui i tasti non arrivano); dopo un riavvio i tasti non
        rimandano lo spegnimento, quindi lo si fa entro keepalive_interval secondi dal
        collegamento, scegliendo se possibile un momento senza tasti premuti.
        """
        if self.keepalive == "wake":
            wake = client.services.get_characteristic(WAKE_CHAR_UUID)
            control = client.services.get_characteristic(WAKE_CONTROL_UUID)
            if wake is None or control is None:
                log.warning("[%s] wake characteristics not found: using the reset instead", self.name)
                self.keepalive, self.keepalive_interval = "reset", RESET_AFTER_S
                return await self.keep_awake(client)
            # Appena collegato: se ora non serve tenerlo sveglio, si aspetta comunque fino a
            # poco prima del minuto, perché un tasto premuto (es. per la musica) può cambiare idea.
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
                # Tra un ciclo e l'altro si controlla spesso se serve ancora (es. MyWhoosh chiuso).
                waited = 0.0
                while waited < self.keepalive_interval:
                    await asyncio.sleep(5)
                    waited += 5
                    if not self.events.keep_awake_allowed():
                        # Ogni ciclo lo tiene acceso ~15 minuti: lo si azzera per farlo spegnere subito.
                        await self.power_off("no need to keep it awake")
                        return

        if self.keepalive == "reset":
            connected_at = time.monotonic()
            # Ogni riavvio consuma pila: lo si fa il più tardi possibile, anticipandolo al massimo
            # di quiet_window secondi per non capitare mentre si stanno premendo tasti.
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
        """La sequenza che tiene acceso il Click per altri ~15 minuti (944 s)."""
        step = "re-enabling notifications"
        try:
            for characteristic in (wake, control):
                # BikeControl riattiva le notifiche a ogni ciclo; bleak non lo permette se sono
                # già attive, quindi si chiede direttamente a CoreBluetooth.
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
            # Il firmware (se noto) aiuta a capire se una versione nuova ha cambiato la sequenza.
            log.warning("[%s] wake sequence failed at step '%s' (firmware %s): %s",
                        self.name, step, self.firmware or "?", e)
            raise
        log.info("[%s] wake sequence ok: kept awake without a restart", self.name)
        await self.read_battery(client)

    async def power_off(self, reason: str) -> None:
        """Fa spegnere il Click il prima possibile. Si riaccende premendo un tasto.

        Il comando POWER_OFF del protocollo Zwift qui risponde NOT_SUPPORTED, quindi si usa
        RESET: azzera i ~15 minuti concessi dall'ultima wake_sequence, e senza un nuovo ciclo
        il Click si spegne da solo dopo circa un minuto.
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
        """Una ricerca e, se trovato, una connessione fino alla disconnessione. True se si è collegato."""
        if self.just_reset and self.device:
            # Dopo un nostro RESET ci si ricollega direttamente, senza cercarlo:
            # macOS aspetta che il Click torni a farsi vedere. Se non va, la
            # volta dopo si cerca normalmente.
            self.just_reset = False
            device = self.device
            self.events.on_status(self.name, STATUS_RESTARTING)
        else:
            self.events.on_status(self.name, STATUS_SEARCHING)
            often = self.events.search_often()
            device = await self.find(timeout=10 if often else 8)
            if device is None:
                if not often:
                    await asyncio.sleep(4)  # ricerca leggera: 8 s di ricerca, 4 di pausa
                return False
            self.device = device

        try:
            await self.connect_and_listen(device)
            return True
        finally:
            # Qualunque sia il motivo della chiusura: niente tasti rimasti "premuti" (es. sterzo).
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
            # La batteria si legge al collegamento e a ogni ciclo per tenerlo sveglio
            # (vedi wake_sequence), più gli aggiornamenti che il Click manda da sé.
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
            except Exception as e:  # BLE su macOS fallisce in molti modi diversi
                log.warning("[%s] error: %s", self.name, e)
            if connected and not self.just_reset:
                self.events.on_status(self.name, STATUS_DISCONNECTED)
            await asyncio.sleep(0.2 if self.just_reset else 1)

