"""Connessione Bluetooth allo Zwift Click v2 e decodifica dei tasti.

Protocollo ricavato dalle parti pubbliche di OpenBikeControl/bikecontrol:
- il Click v2 annuncia il servizio 0xFC82 e i dati produttore 0x094A (Zwift),
  col primo byte che dice il lato: 0x0A destro, 0x0B sinistro
- handshake: si scrive "RideOn" sulla caratteristica sync RX
- ogni messaggio inizia con un opcode; 0x23 = stato dei tasti (protobuf
  RideKeyPadStatus, campo 1 = bitmask, bit a 0 = tasto premuto)
- il sinistro inoltra anche tasti e batteria del destro: ogni lato tiene solo i suoi tasti,
  e la batteria si legge dalla caratteristica standard 0x2A19 di ciascun Click

Uso da terminale:
    python ble.py --scan     cerca i Click nelle vicinanze
    python ble.py --debug    si collega e stampa i tasti premuti
"""

import argparse
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
SIDE_NAMES = {SIDE_RIGHT: "destro", SIDE_LEFT: "sinistro"}

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
            raise ValueError(f"wire type {wire_type} non gestito")
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
    BleakBluetoothNotAvailableReason.POWERED_OFF: "Bluetooth spento",
    BleakBluetoothNotAvailableReason.DENIED_BY_USER: "permesso Bluetooth mancante",
    BleakBluetoothNotAvailableReason.DENIED_BY_SYSTEM: "permesso Bluetooth mancante",
    BleakBluetoothNotAvailableReason.DENIED_BY_UNKNOWN: "permesso Bluetooth mancante",
}


@dataclass
class ClickSide:
    """Un Click v2 (destro o sinistro): lo cerca, si collega e si ricollega da solo."""

    side: int
    events: ClickEvents
    debug: bool = False
    # "wake": sequenza che lo tiene acceso ~15 minuti. "reset": riavvio prima del minuto (riserva).
    keepalive: str | None = None
    keepalive_interval: float = 300.0  # secondi tra un rinnovo e l'altro
    pressed: set[str] = field(default_factory=set)
    last_message: float = 0.0
    device: BLEDevice | None = None
    just_reset: bool = False
    explored: bool = False
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
        if self.debug:
            log.info("[%s] ricevuto %s", self.name, data.hex(" "))
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
            log.warning("[%s] messaggio non decodificato %s: %s", self.name, data.hex(" "), e)

    def update_buttons(self, now: set[str]) -> None:
        for name in sorted(now - self.pressed):
            self.events.on_press(name)
        for name in sorted(self.pressed - now):
            self.events.on_release(name)
        self.pressed = now

    async def listen_to_everything(self, client: BleakClient) -> None:
        """Solo debug: elenca servizi e caratteristiche, legge i valori e ascolta tutte le notifiche."""
        known = {ASYNC_CHAR_UUID, SYNC_TX_CHAR_UUID}
        for service in client.services:
            log.info("[%s] servizio %s", self.name, service.uuid)
            for char in service.characteristics:
                descriptors = ", ".join(f"{d.uuid[4:8]}@0x{d.handle:04x}" for d in char.descriptors)
                log.info("[%s]   handle 0x%04x %s (%s) %s", self.name, char.handle, char.uuid[:8],
                         ", ".join(char.properties), descriptors)
                if char.uuid in known:
                    continue
                if "read" in char.properties:
                    try:
                        value = bytes(await client.read_gatt_char(char))
                        text = value.decode("utf-8", "replace") if value.isascii() else ""
                        log.info("[%s] %s = %s %s", self.name, char.uuid, value.hex(" "), text)
                    except Exception as e:
                        log.info("[%s] %s non leggibile: %s", self.name, char.uuid, e)
                if "notify" in char.properties or "indicate" in char.properties:
                    uuid = char.uuid
                    try:
                        await client.start_notify(
                            char, lambda _, d, uuid=uuid: log.info("[%s] da %s: %s", self.name, uuid, bytes(d).hex(" "))
                        )
                        log.info("[%s] ascolto anche %s (%s)", self.name, uuid, ", ".join(char.properties))
                    except Exception as e:
                        log.info("[%s] non riesco ad ascoltare %s: %s", self.name, uuid, e)

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
                log.warning("[%s] caratteristiche per tenerlo sveglio non trovate: uso il riavvio", self.name)
                self.keepalive, self.keepalive_interval = "reset", RESET_AFTER_S
                return await self.keep_awake(client)
            # Appena collegato: se ora non serve tenerlo sveglio, si aspetta comunque fino a
            # poco prima del minuto, perché un tasto premuto (es. per la musica) può cambiare idea.
            waited = 0.0
            while not self.events.keep_awake_allowed():
                if waited == 0:
                    log.info("[%s] per ora non serve tenerlo sveglio", self.name)
                if waited >= 50:
                    log.info("[%s] nessun tasto: si spegnerà da solo", self.name)
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
                        await self.power_off("nessun bisogno di tenerlo sveglio")
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
                await self.power_off("nessun bisogno di tenerlo sveglio")
                return
            log.info("[%s] collegato da %.0f s, tasti fermi da %.0f s: riavvio il Click", self.name, age, idle)
            self.just_reset = True
            await client.write_gatt_char(SYNC_RX_CHAR_UUID, bytes([OPCODE_RESET]), response=False)

    async def read_battery(self, client: BleakClient) -> None:
        try:
            self.events.on_battery(self.name, (await client.read_gatt_char(BATTERY_LEVEL_UUID))[0])
        except Exception as e:
            log.info("[%s] batteria non leggibile: %s", self.name, e)

    async def read_firmware(self, client: BleakClient) -> None:
        try:
            version = bytes(await client.read_gatt_char(FIRMWARE_UUID)).decode("utf-8", "replace").strip()
        except Exception as e:
            log.info("[%s] firmware non leggibile: %s", self.name, e)
            return
        log.info("[%s] firmware %s", self.name, version)
        self.events.on_firmware(self.name, version)

    async def wake_sequence(self, client: BleakClient, wake, control) -> None:
        """La sequenza che tiene acceso il Click per altri ~15 minuti (944 s)."""
        for characteristic in (wake, control):
            # BikeControl riattiva le notifiche a ogni ciclo; bleak non lo permette se sono
            # già attive, quindi si chiede direttamente a CoreBluetooth.
            delegate = getattr(getattr(client, "_backend", None), "_delegate", None)
            if delegate is not None:
                delegate.peripheral.setNotifyValue_forCharacteristic_(True, characteristic.obj)
        await asyncio.sleep(0.1)
        await client.write_gatt_char(control, b"\x01", response=False)
        await asyncio.sleep(0.06)
        await client.write_gatt_char(wake, WAKE_MESSAGE, response=False)
        await asyncio.sleep(0.06)
        await client.write_gatt_char(control, b"\x03", response=False)
        log.info("[%s] tenuto sveglio senza riavvio", self.name)
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
        log.info("[%s] %s: lo faccio spegnere", self.name, reason)
        self.rest_until = time.monotonic() + 70
        try:
            await client.write_gatt_char(SYNC_RX_CHAR_UUID, bytes([OPCODE_RESET]), response=False)
        except Exception as e:
            log.info("[%s] spegnimento non riuscito: %s", self.name, e)

    async def run_once(self) -> bool:
        """Una ricerca e, se trovato, una connessione fino alla disconnessione. True se si è collegato."""
        if self.just_reset and self.device:
            # Dopo un nostro RESET ci si ricollega direttamente, senza cercarlo:
            # macOS aspetta che il Click torni a farsi vedere. Se non va, la
            # volta dopo si cerca normalmente.
            self.just_reset = False
            device = self.device
            self.events.on_status(self.name, "riavvio")
        else:
            self.events.on_status(self.name, "in cerca")
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
        log.info("[%s] mi collego a %s", self.name, device.name or device.address)
        async with client:
            self.client = client
            await client.start_notify(ASYNC_CHAR_UUID, lambda _, d: self.handle(bytes(d)))
            await client.start_notify(SYNC_TX_CHAR_UUID, lambda _, d: self.handle(bytes(d)))
            if self.debug and not self.explored:
                await self.listen_to_everything(client)
                self.explored = True
            elif self.keepalive == "wake":
                for uuid in (WAKE_CHAR_UUID, WAKE_CONTROL_UUID):
                    characteristic = client.services.get_characteristic(uuid)
                    if characteristic is not None:
                        await client.start_notify(characteristic, lambda c, d: log.debug(
                            "[%s] risposta %s: %s", self.name, c.uuid[:8], bytes(d).hex(" ")))
            await client.write_gatt_char(SYNC_RX_CHAR_UUID, RIDE_ON, response=False)
            if self.side == SIDE_LEFT:
                await client.write_gatt_char(SYNC_RX_CHAR_UUID, UNLOCKED_HELLO, response=False)
            self.last_message = time.monotonic()
            self.events.on_status(self.name, "collegato")
            # La batteria si legge al collegamento e a ogni ciclo per tenerlo sveglio
            # (vedi wake_sequence), più gli aggiornamenti che il Click manda da sé.
            await self.read_battery(client)
            await self.read_firmware(client)
            if not self.debug:
                try:
                    await client.start_notify(BATTERY_LEVEL_UUID,
                                              lambda _, d: d and self.events.on_battery(self.name, d[0]))
                except Exception as e:
                    log.info("[%s] notifiche batteria non disponibili: %s", self.name, e)
            keepalive = asyncio.create_task(self.keep_awake(client)) if self.keepalive else None
            if keepalive:
                keepalive.add_done_callback(self._log_keepalive_error)
            try:
                await disconnected.wait()
            finally:
                if keepalive:
                    keepalive.cancel()
        log.info("[%s] disconnesso", self.name)

    def _log_keepalive_error(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.warning("[%s] errore nel tenerlo sveglio: %s", self.name, task.exception())

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
                problem = BLUETOOTH_PROBLEMS.get(e.reason, "Bluetooth non disponibile")
                log.warning("[%s] %s", self.name, problem)
                self.events.on_status(self.name, problem)
                await asyncio.sleep(5)
                continue
            except Exception as e:  # BLE su macOS fallisce in molti modi diversi
                log.warning("[%s] errore: %s", self.name, e)
            if connected and not self.just_reset:
                self.events.on_status(self.name, "disconnesso")
            await asyncio.sleep(0.2 if self.just_reset else 1)


async def scan(seconds: float) -> None:
    print(f"Cerco per {seconds:.0f} secondi... premi un tasto del Click per svegliarlo.")
    seen: dict[str, tuple[str, int | None, int]] = {}

    def on_detect(device: BLEDevice, adv: AdvertisementData) -> None:
        if ZWIFT_MANUFACTURER_ID in adv.manufacturer_data or RIDE_SERVICE_UUID in adv.service_uuids:
            seen[device.address] = (device.name or "?", click_side(adv), adv.rssi)

    async with BleakScanner(on_detect):
        await asyncio.sleep(seconds)

    if not seen:
        print("Nessun dispositivo Zwift trovato.")
    for address, (name, side, rssi) in seen.items():
        label = SIDE_NAMES.get(side, "lato sconosciuto") if side is not None else "non Click v2"
        print(f"  {name:20} {label:16} segnale {rssi} dBm  ({address})")


async def debug(use_left: bool, keepalive: str | None, interval: float) -> None:
    events = ClickEvents(
        on_press=lambda n: print(f"  ▼ {n}"),
        on_release=lambda n: print(f"  ▲ {n}"),
        on_battery=lambda s, lvl: print(f"  batteria {s}: {lvl}%"),
        on_status=lambda s, st: print(f"[{s}] {st}"),
    )
    sides = [ClickSide(side, events, debug=True, keepalive=keepalive, keepalive_interval=interval)
             for side in ([SIDE_RIGHT, SIDE_LEFT] if use_left else [SIDE_RIGHT])]
    await asyncio.gather(*(s.run_forever() for s in sides))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scan", action="store_true", help="cerca i Click nelle vicinanze")
    parser.add_argument("--debug", action="store_true", help="collegati e stampa i tasti premuti")
    parser.add_argument("--keepalive", choices=["wake", "reset"], help="con --debug, tieni sveglio il Click")
    parser.add_argument("--interval", type=float, default=300, help="secondi tra un rinnovo e l'altro")
    parser.add_argument("--left", action="store_true", help="con --debug, collega anche il Click sinistro")
    parser.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    try:
        if args.debug:
            asyncio.run(debug(args.left, args.keepalive, args.interval))
        else:
            asyncio.run(scan(args.seconds))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
