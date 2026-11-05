"""OpenBikeControl: the Mac presents itself to MyWhoosh as a network remote control.

Open protocol (MIT): https://github.com/OpenBikeControl/openbikecontrol-protocol
- Bonjour announcement "_openbikecontrol._tcp", MyWhoosh connects over TCP
- us -> app:  [0x01, id, state, id, state, ...]  state 1 = pressed, 0 = released
              [0x02, battery, 1]                 device status
- app -> us:  [0x04, version, len, app_id, len, app_version, n, id...]  accepted commands
              [0x03, pattern, duration, intensity] vibration (the Click v2 does not vibrate: ignored)

The announcement uses NSNetService, i.e. the system Bonjour: MyWhoosh resolves the address
by asking it directly. NSNetService must be published from a thread with a run loop (the
app's main one); the TCP server runs instead in the Bluetooth asyncio loop.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("click2whoosh")

SERVICE_TYPE = "_openbikecontrol._tcp."
ADVERTISED_NAME = "Click2Whoosh"  # the name MyWhoosh shows in its list
SERVICE_UUID = "d273f680-d548-419d-b9d1-fa0472345229"
PREFERRED_PORT = 36867

MSG_BUTTONS = 0x01
MSG_STATUS = 0x02
MSG_HAPTIC = 0x03
MSG_APP_INFO = 0x04
MSG_HEARTBEAT = 0x00  # MyWhoosh sends it every ~4 s; it is not in the spec and expects no reply

# Names used in config.toml ("obc:shift_up") -> protocol IDs.
BUTTONS = {
    "shift_up": 0x01, "shift_down": 0x02, "gear_set": 0x03,
    "up": 0x10, "down": 0x11, "left": 0x12, "right": 0x13,
    "select": 0x14, "back": 0x15, "menu": 0x16, "home": 0x17,
    "steer_left": 0x18, "steer_right": 0x19,
    "emote": 0x20, "screenshot": 0x24,
    "erg_up": 0x30, "erg_down": 0x31, "skip_interval": 0x32, "pause": 0x33, "resume": 0x34,
    "lap": 0x35, "previous_interval": 0x36, "uturn": 0x37, "change_mode": 0x38,
    "camera": 0x40, "hud": 0x44, "map": 0x45,
}
BUTTON_NAMES = {v: k for k, v in BUTTONS.items()}


def describe(ids: list[int]) -> str:
    return ", ".join(BUTTON_NAMES.get(i, f"0x{i:02X}") for i in ids) or "all"


@dataclass
class AppInfo:
    app_id: str
    version: str
    buttons: list[int]  # empty = the app accepts everything


def parse_app_info(data: bytes) -> tuple[AppInfo | None, int]:
    """Decodes a 0x04 message at the start of data. Returns (info, bytes used);
    (None, 0) if the message has not fully arrived yet."""
    try:
        pos = 2  # type, format version
        n = data[pos]; app_id = data[pos + 1:pos + 1 + n].decode("utf-8", "replace"); pos += 1 + n
        n = data[pos]; version = data[pos + 1:pos + 1 + n].decode("utf-8", "replace"); pos += 1 + n
        n = data[pos]; buttons = list(data[pos + 1:pos + 1 + n]); pos += 1 + n
    except IndexError:
        return None, 0
    if pos > len(data):
        return None, 0
    return AppInfo(app_id, version, buttons), pos


@dataclass
class OBCServer:
    """TCP server MyWhoosh connects to. Thread-safe: send() can be called from any thread."""

    loop: asyncio.AbstractEventLoop | None = None
    port: int = 0
    app: AppInfo | None = None
    battery: int = 0xFF
    writers: set = field(default_factory=set)
    # Called (from the Bluetooth thread) when an app connects (True) or the last one disconnects (False).
    on_change: Callable[[bool], None] = lambda connected: None

    @property
    def connected(self) -> bool:
        return bool(self.writers)

    def supports(self, button_id: int) -> bool:
        return self.app is None or not self.app.buttons or button_id in self.app.buttons

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        # We try fixed ports one after another (not port 0, which would give
        # different ports to IPv4 and IPv6 while only one can be announced).
        for port in range(PREFERRED_PORT, PREFERRED_PORT + 20):
            try:
                self.tcp = await asyncio.start_server(self.handle, port=port)
                break
            except OSError:
                log.info("port %d is busy, trying the next one", port)
        else:
            raise OSError("no free port for OpenBikeControl")
        self.port = port
        log.info("OpenBikeControl listening on port %d", self.port)

    async def stop(self) -> None:
        self.tcp.close()
        for writer in list(self.writers):
            writer.close()
        await self.tcp.wait_closed()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("OpenBikeControl: connected %s", peer[0] if peer else "?")
        self.writers.add(writer)
        if len(self.writers) == 1:
            self.on_change(True)
        writer.write(bytes([MSG_STATUS, self.battery, 0x01]))
        buffer = b""
        try:
            while data := await reader.read(1024):
                buffer += data
                buffer = self.consume(buffer)
        except (ConnectionError, OSError) as e:
            log.info("OpenBikeControl: connection closed (%s)", e)
        finally:
            self.writers.discard(writer)
            if not self.writers:
                self.app = None
                self.on_change(False)
            writer.close()
            log.info("OpenBikeControl: disconnected %s", peer[0] if peer else "?")

    def consume(self, buffer: bytes) -> bytes:
        """Processes the complete messages in buffer and returns what is left over."""
        while buffer:
            kind = buffer[0]
            if kind == MSG_APP_INFO:
                info, used = parse_app_info(buffer)
                if not used:
                    return buffer
                self.app = info
                log.info("OpenBikeControl: app %s %s, accepted commands: %s",
                         info.app_id, info.version, describe(info.buttons))
                buffer = buffer[used:]
            elif kind == MSG_HEARTBEAT:
                buffer = buffer[1:]
            elif kind == MSG_HAPTIC:
                if len(buffer) < 4:
                    return buffer
                buffer = buffer[4:]
            else:
                log.info("OpenBikeControl: unknown message %s", buffer.hex(" "))
                return b""
        return buffer

    def send(self, button_ids: list[int], pressed: bool) -> None:
        """One or more commands in the same message: the app uses the ones it needs."""
        message = [MSG_BUTTONS]
        for button_id in button_ids:
            if not self.supports(button_id):
                log.info("%s not accepted by %s", BUTTON_NAMES.get(button_id, hex(button_id)), self.app.app_id)
            message += [button_id, 1 if pressed else 0]
        self._write(bytes(message))

    def set_battery(self, level: int) -> None:
        self.battery = max(0, min(100, level))
        self._write(bytes([MSG_STATUS, self.battery, 0x01]))

    def _write(self, message: bytes) -> None:
        if self.loop is None:
            return

        def write() -> None:
            for writer in list(self.writers):
                if not writer.is_closing():
                    writer.write(message)

        self.loop.call_soon_threadsafe(write)


server = OBCServer()


class Advertiser:
    """System Bonjour announcement. Create and use it on the main thread."""

    def __init__(self, port: int, name: str = ADVERTISED_NAME):
        from Foundation import NSNetService

        device_id = f"{uuid.getnode():012x}"
        txt = {
            "version": "1", "id": device_id, "name": name, "service-uuids": SERVICE_UUID,
            "manufacturer": "Click2Whoosh", "model": "Zwift Click v2",
        }
        self.service = NSNetService.alloc().initWithDomain_type_name_port_("local.", SERVICE_TYPE, name, port)
        self.service.setTXTRecordData_(
            NSNetService.dataFromTXTRecordDictionary_({k: v.encode() for k, v in txt.items()})
        )
        self.service.publish()
        log.info("Bonjour announcement %s on port %d", SERVICE_TYPE, port)

    def stop(self) -> None:
        self.service.stop()

    def republish(self) -> None:
        """After the Mac sleeps the announcement may be lost: it is made again."""
        self.service.stop()
        self.service.publish()
        log.info("Bonjour announcement renewed")

