"""click2whoosh: usa lo Zwift Click v2 con MyWhoosh su Mac.

Avvio:
    .venv/bin/python app.py

Compare un'icona nella barra dei menu. Il Bluetooth gira in un thread a parte;
la barra dei menu legge lo stato una volta al secondo.

La configurazione sta in ~/Library/Application Support/Click2Whoosh/config.toml
(creata al primo avvio copiando config.toml di questa cartella), il registro in
~/Library/Logs/Click2Whoosh/click2whoosh.log.
"""

import asyncio
import logging
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

import objc
import rumps
from AppKit import NSWorkspace
from Foundation import NSBundle, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer
from PyObjCTools import AppHelper

import icons
import login
import obc
from actions import DEFAULT_REPEAT_BUTTONS, Action, describe, frontmost_app, play_shift_sound, repeatable
from ble import BUTTON_LABELS, SIDE_LEFT, SIDE_RIGHT, TESTED_FIRMWARE, ClickEvents, ClickSide

DEFAULT_CONFIG = Path(__file__).with_name("config.toml")
CONFIG_PATH = Path.home() / "Library" / "Application Support" / "Click2Whoosh" / "config.toml"
LOG_PATH = Path.home() / "Library" / "Logs" / "Click2Whoosh" / "click2whoosh.log"
TARGET_APP = "MyWhoosh"  # basta che il nome dell'app lo contenga ("MyWhoosh Indoor Cycling App")
WAKE_INTERVAL_S = 300  # ogni sequenza "wake" tiene acceso il Click ~15 minuti: la si rinnova ogni 5
SLEEP_AFTER_FRONT_MIN = 60  # senza tasti, con MyWhoosh in primo piano (magari stai pedalando in ERG)
SLEEP_AFTER_MIN = 10  # senza tasti, con MyWhoosh dietro altre finestre
REPEAT_DELAY_S = 0.4  # tenendo premuto un tasto ripetibile, dopo quanto parte la ripetizione
REPEAT_INTERVAL_S = 0.2
CONNECTED_STATES = ("collegato", "riavvio")
WAITING_STATES = ("in cerca", "disconnesso")
log = logging.getLogger("click2whoosh")


def friendly_status(status: str) -> str:
    """Lo stato del Click a parole, senza l'altalena in cerca/disconnesso mentre dorme."""
    if status in CONNECTED_STATES:
        return "collegato"
    if status in WAITING_STATES:
        return "in attesa (premi un tasto)"
    return status


def notify(title: str, message: str) -> None:
    """Notifica di macOS. Nell'app impacchettata passa da UNUserNotificationCenter, così compare
    il logo di Click2Whoosh; da Terminale non c'è un'app a cui intestarla e si usa osascript
    (rumps.notification usa NSUserNotification, che su macOS recenti non arriva più)."""
    if login.app_bundle() is None:
        notify_with_osascript(title, message)
        return
    from UserNotifications import (UNAuthorizationOptionAlert, UNAuthorizationOptionSound, UNMutableNotificationContent,
                                   UNNotificationRequest, UNUserNotificationCenter)
    center = UNUserNotificationCenter.currentNotificationCenter()
    if center.delegate() is None:
        center.setDelegate_(_NotificationDelegate.alloc().init())

    def authorized(granted: bool, error) -> None:
        # La prima volta macOS chiede il permesso; poi risponde subito.
        if error is not None:
            log.info("notifiche di sistema non disponibili (%s): uso osascript", error)
            notify_with_osascript(title, message)
        elif not granted:
            log.info("notifiche non permesse: %s", message)
        else:
            content = UNMutableNotificationContent.alloc().init()
            content.setTitle_(title)
            content.setBody_(message)
            request = UNNotificationRequest.requestWithIdentifier_content_trigger_(str(uuid.uuid4()), content, None)
            center.addNotificationRequest_withCompletionHandler_(request, None)

    center.requestAuthorizationWithOptions_completionHandler_(
        UNAuthorizationOptionAlert | UNAuthorizationOptionSound, authorized)


def notify_with_osascript(title: str, message: str) -> None:
    quote = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{quote(message)}" with title "{quote(title)}"'
    subprocess.Popen(["osascript", "-e", script])


class _NotificationDelegate(NSObject):
    """Mostra la notifica anche quando Click2Whoosh è in primo piano (es. con le Impostazioni aperte)."""

    def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, center, notification, handler):
        from UserNotifications import UNNotificationPresentationOptionBanner, UNNotificationPresentationOptionList
        handler(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList)


def mywhoosh_running() -> bool:
    return any(TARGET_APP.lower() in (app.localizedName() or "").lower()
               for app in NSWorkspace.sharedWorkspace().runningApplications())


def mywhoosh_in_front() -> bool:
    return TARGET_APP.lower() in frontmost_app().lower()


class Config:
    """config.toml: solo ciò che ha senso cambiare (le stesse cose della finestra Impostazioni)."""

    def __init__(self, path: Path):
        with path.open("rb") as f:
            raw = tomllib.load(f)
        general = raw.get("general", {})
        limits = {"long_press_ms": (600, 200, 3000), "double_press_ms": (300, 100, 1000),
                  "battery_warning": (20, 0, 100)}
        values = {}
        for key, (default, low, high) in limits.items():
            values[key] = general.get(key, default)
            if not low <= values[key] <= high:
                raise ValueError(f"{key} deve stare tra {low} e {high}")
        self.long_press_s: float = values["long_press_ms"] / 1000
        self.double_window_s: float = values["double_press_ms"] / 1000
        self.battery_warning: int = values["battery_warning"]
        self.use_left_side: bool = general.get("use_left_side", True)
        self.shift_sound: bool = general.get("shift_sound", True)
        self.repeat: set[str] = set(general.get("repeat_buttons", DEFAULT_REPEAT_BUTTONS))
        self.buttons = {name: Action(spec) for name, spec in raw.get("buttons", {}).items()}
        self.long_press = {name: Action(spec) for name, spec in raw.get("long_press", {}).items()}
        self.double_press = {name: Action(spec) for name, spec in raw.get("double_press", {}).items()}


class Dispatcher:
    """Trasforma pressioni e rilasci dei tasti in azioni sul Mac."""

    def __init__(self, config: Config):
        self.config = config
        self.last_button = "—"
        self.last_press = time.monotonic()  # all'avvio conta come attività
        self.active: dict[str, Action] = {}  # azioni "hold" premute, da rilasciare
        self.long_timers: dict[str, threading.Timer] = {}
        self.long_fired: set[str] = set()
        self.single_timers: dict[str, threading.Timer] = {}
        self.double_fired: set[str] = set()
        self.repeats: dict[str, threading.Event] = {}  # tasti che stanno ripetendo; set() li ferma
        self.listener = None  # chiamata a ogni pressione/rilascio fisico: (nome, premuto)
        # Se True i tasti non eseguono azioni (finestra Impostazioni in primo piano: servono a selezionare).
        self.capture = lambda: False
        self.captured: set[str] = set()

    def allowed(self, action: Action) -> bool:
        if action.needs_target_app and not mywhoosh_in_front():
            log.info("ignoro %s: in primo piano c'è '%s', non %s", action, frontmost_app(), TARGET_APP)
            return False
        return True

    def run(self, name: str, action: Action) -> None:
        button, _, how = name.partition(" ")
        label = BUTTON_LABELS.get(button, button) + (f" {how}" if how else "")
        self.last_button = f"{label} → {describe(action.spec)}"
        log.info("%s (%s)", self.last_button, action)
        if self.allowed(action):
            action.press()
            if action.stays_pressed:
                self.active[name] = action
            direction = action.shift_direction
            if direction and self.config.shift_sound:
                play_shift_sound(direction)

    def tap(self, name: str, action: Action) -> None:
        """Preme e rilascia subito (azioni decise al rilascio o dopo un'attesa)."""
        self.run(name, action)
        self.release_active(name)

    def on_press(self, name: str) -> None:
        self.last_press = time.monotonic()
        if self.listener:
            self.listener(name, True)
        if self.capture():
            self.captured.add(name)
            self.last_button = f"{BUTTON_LABELS.get(name, name)} → solo illuminato (finestra in primo piano)"
            log.info(self.last_button)
            return
        waiting = self.single_timers.pop(name, None)
        if waiting:
            # Secondo clic entro il tempo: doppio clic.
            waiting.cancel()
            self.double_fired.add(name)
            self.tap(f"{name} doppio", self.config.double_press[name])
            return
        if name in self.config.long_press:
            self.long_fired.discard(name)
            timer = threading.Timer(self.config.long_press_s, self.fire_long, args=(name,))
            self.long_timers[name] = timer
            timer.start()
            return
        if name in self.config.double_press:
            return  # si decide al rilascio
        if name not in self.config.buttons:
            self.last_button = f"{BUTTON_LABELS.get(name, name)} → niente"
            return
        action = self.config.buttons[name]
        self.run(name, action)
        if name in self.config.repeat and repeatable(action.spec):
            stop = threading.Event()
            self.repeats[name] = stop
            threading.Thread(target=self.repeat, args=(name, action, stop), daemon=True).start()

    def repeat(self, name: str, action: Action, stop: threading.Event) -> None:
        """Tenendo premuto il tasto, l'azione si ripete (es. più cambi di marcia di fila)."""
        if stop.wait(REPEAT_DELAY_S):
            return
        while not stop.is_set():
            if action.stays_pressed:
                action.release()  # ogni ripetizione deve essere una pressione nuova
            self.run(name, action)
            stop.wait(REPEAT_INTERVAL_S)

    def fire_long(self, name: str) -> None:
        self.long_fired.add(name)
        self.tap(f"{name} lungo", self.config.long_press[name])

    def on_release(self, name: str) -> None:
        if self.listener:
            self.listener(name, False)
        if name in self.captured:
            self.captured.discard(name)
            return
        stop = self.repeats.pop(name, None)
        if stop:
            stop.set()
        timer = self.long_timers.pop(name, None)
        if timer:
            timer.cancel()
        if name in self.double_fired or name in self.long_fired:
            self.double_fired.discard(name)
            self.long_fired.discard(name)
        elif name in self.config.double_press:
            # Aspetta un eventuale secondo clic prima di decidere che era un clic singolo.
            single = self.config.buttons.get(name)
            timer = threading.Timer(self.config.double_window_s, self.fire_single, args=(name, single))
            self.single_timers[name] = timer
            timer.start()
        elif timer and name in self.config.buttons:
            # Pressione breve su un tasto con pressione lunga: azione normale, come un tocco.
            self.tap(name, self.config.buttons[name])
        self.release_active(name)

    def fire_single(self, name: str, action: Action | None) -> None:
        self.single_timers.pop(name, None)
        if action:
            self.tap(name, action)

    def release_active(self, name: str) -> None:
        # Si rilascia sempre, anche se nel frattempo MyWhoosh non è più in primo piano,
        # altrimenti il tasto resterebbe premuto.
        action = self.active.pop(name, None)
        if action:
            action.release()


class ClickBridge:
    """Thread con il loop asyncio che tiene collegati i Click e il server OpenBikeControl."""

    def __init__(self, dispatcher: Dispatcher, config: Config, use_bluetooth: bool = True):
        self.dispatcher = dispatcher
        self.config = config
        self.use_bluetooth = use_bluetooth
        self.status: dict[str, str] = {"destro": "in cerca" if use_bluetooth else "Bluetooth non usato"}
        if config.use_left_side:
            self.status["sinistro"] = "in cerca"
        self.battery: dict[str, int] = {}
        self.warned: set[str] = set()  # batteria scarica già segnalata
        self.warned_firmware: set[str] = set()
        self.mac_sleeping = False  # durante lo stop del Mac non si tiene sveglio il Click
        self.last_reason = ""  # per scrivere nel registro il motivo solo quando cambia
        self.sides: list[ClickSide] = []
        self.loop: asyncio.AbstractEventLoop | None = None  # il loop del Bluetooth, quando parte
        # Al primo avvio la ricerca del Click (e quindi la richiesta del permesso Bluetooth)
        # aspetta il pulsante "Consenti" della finestra di benvenuto.
        self.bluetooth_go = threading.Event()
        self.events = ClickEvents(
            on_press=dispatcher.on_press,
            on_release=dispatcher.on_release,
            on_battery=self.on_battery,
            on_firmware=self.on_firmware,
            on_status=self.on_status,
            keep_awake_allowed=self.keep_awake_allowed,
            search_often=self.search_often,
        )

    def on_status(self, side: str, status: str) -> None:
        if self.status.get(side) != status:
            log.info("[%s] %s", side, status)
        self.status[side] = status

    def on_battery(self, side: str, level: int) -> None:
        if self.battery.get(side) != level:
            log.info("[%s] batteria %d%%", side, level)
        self.battery[side] = level
        obc.server.set_battery(min(self.battery.values()))
        if level < self.config.battery_warning and side not in self.warned:
            self.warned.add(side)
            notify("Zwift Click", f"Batteria del Click {side} al {level}%")

    def on_firmware(self, side: str, version: str) -> None:
        if version != TESTED_FIRMWARE and side not in self.warned_firmware:
            self.warned_firmware.add(side)
            notify("Firmware del Click cambiato",
                   f"Click {side}: {version} invece di {TESTED_FIRMWARE}. Se qualcosa non va, è per questo.")

    def search_often(self) -> bool:
        """Ricerca continua con MyWhoosh aperto; altrimenti leggera (il Mac consuma meno)."""
        return not self.mac_sleeping and mywhoosh_running()

    def keep_awake_allowed(self) -> bool:
        """Tenerlo sveglio consuma la pila: solo con MyWhoosh aperto e tasti premuti di recente.

        Con MyWhoosh in primo piano probabilmente stai pedalando (magari in ERG, senza
        cambiare marcia), quindi si aspetta di più prima di lasciarlo spegnere.
        """
        reason = self.sleep_reason()
        if reason and reason != self.last_reason:
            log.info("non serve tenerlo sveglio: %s", reason)
        self.last_reason = reason
        return not reason

    def sleep_reason(self) -> str:
        """Perché il Click può spegnersi; stringa vuota se va tenuto sveglio."""
        if self.mac_sleeping:
            return "il Mac va in stop"
        idle_min = (time.monotonic() - self.dispatcher.last_press) / 60
        if idle_min < 2:
            return ""  # lo stai usando adesso (magari solo per la musica)
        if not mywhoosh_running():
            return f"{TARGET_APP} non è aperto e nessun tasto da almeno 2 minuti"
        in_front = mywhoosh_in_front()
        limit = SLEEP_AFTER_FRONT_MIN if in_front else SLEEP_AFTER_MIN
        if idle_min >= limit:
            where = "in primo piano" if in_front else "in secondo piano"
            return f"nessun tasto da {limit} minuti con {TARGET_APP} {where}"
        return ""

    def power_off_all(self, reason: str) -> None:
        """Spegne i Click collegati; da chiamare da un thread diverso da quello del Bluetooth."""
        if self.loop is None:
            return

        async def power_off() -> None:
            # gather va chiamato dentro il loop del Bluetooth, non dal thread che chiama.
            await asyncio.gather(*(side.power_off(reason) for side in self.sides))

        future = asyncio.run_coroutine_threadsafe(power_off(), self.loop)
        try:
            future.result(timeout=2)
        except Exception as e:
            log.info("spegnimento all'uscita non riuscito: %s", e)

    def start(self) -> None:
        """Avvia il thread; restituisce quando il server OpenBikeControl (se attivo) ha la sua porta."""
        self.obc_ready = threading.Event()
        threading.Thread(target=self.run, daemon=True).start()
        self.obc_ready.wait(5)

    def allow_bluetooth(self) -> None:
        self.bluetooth_go.set()

    def run(self) -> None:
        sides = [SIDE_RIGHT, SIDE_LEFT] if self.config.use_left_side else [SIDE_RIGHT]
        self.sides = [ClickSide(side, self.events, keepalive="wake", keepalive_interval=WAKE_INTERVAL_S)
                      for side in sides] if self.use_bluetooth else []

        async def main() -> None:
            self.loop = asyncio.get_running_loop()
            try:
                await obc.server.start()
            except OSError as e:
                log.error("OpenBikeControl non avviato: %s", e)
            self.obc_ready.set()
            await asyncio.to_thread(self.bluetooth_go.wait)
            await asyncio.gather(*(s.run_forever() for s in self.sides), asyncio.Event().wait())

        asyncio.run(main())


class MenuBarApp(rumps.App):
    def __init__(self, bridge: ClickBridge, dispatcher: Dispatcher, first_run: bool = False):
        # "Esci" è una voce nostra: quella di rumps chiuderebbe senza spegnere il Click.
        super().__init__("Click2Whoosh", quit_button=None)
        self.bridge = bridge
        self.dispatcher = dispatcher
        self.advertiser = None  # annuncio Bonjour per MyWhoosh: vedi start_advertising()

        from onboarding import OnboardingWindow
        from settings import SettingsWindow
        self.settings = SettingsWindow(CONFIG_PATH, self.status, self.config_saved, lambda: self.dispatcher.config)
        self.onboarding = OnboardingWindow(self.status, self.button_summary, bridge.allow_bluetooth,
                                           self.start_advertising, lambda: self.advertiser is not None,
                                           self.settings.show)
        dispatcher.listener = self.pressed
        # Con una delle due finestre in primo piano i tasti del Click si illuminano e basta.
        dispatcher.capture = lambda: self.settings.focused or self.onboarding.focused
        self.watcher = SystemWatcher(bridge, dispatcher, self.republish)

        self.header = rumps.MenuItem(f"Click2Whoosh {app_version()}")
        self.side_items = {side: rumps.MenuItem("") for side in bridge.status}
        self.last_item = rumps.MenuItem("")
        settings_item = rumps.MenuItem("Impostazioni…", callback=lambda _: self.settings.show(), key=",")
        guide_item = rumps.MenuItem("Guida introduttiva…", callback=lambda _: self.onboarding.show())
        log_item = rumps.MenuItem("Apri registro", callback=self.open_log)
        self.login_item = rumps.MenuItem("Avvia con il Mac", callback=self.toggle_login)
        self.menu = [
            self.header, *self.side_items.values(), self.last_item, None,
            settings_item, guide_item, log_item, None,
            self.login_item, None,
            rumps.MenuItem("Esci", callback=self.quit, key="q"),
        ]
        set_image(settings_item, icons.symbol("slider.horizontal.3"))
        set_image(guide_item, icons.symbol("questionmark.circle"))
        set_image(log_item, icons.symbol("doc.text"))
        set_image(self.header, icons.logo(14))
        for item in (self.header, self.last_item):
            item._menuitem.setEnabled_(False)  # righe informative, non cliccabili
        self.login_item.state = login.is_enabled()
        self.refresh(None)
        # I timer di rumps si fermano mentre il menu è aperto: questo gira in tutti i modi
        # del run loop, così stato e batteria si aggiornano anche col menu aperto.
        self.ticker = _Ticker.alloc().initWithCallback_(lambda: self.refresh(None))
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self.ticker, "tick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        if first_run:
            # Bluetooth e rete partono dai pulsanti della finestra, così macOS chiede un permesso
            # alla volta. Dopo l'avvio dell'app: se la finestra nasce prima, WebKit non carica la pagina.
            AppHelper.callLater(1.0, self.onboarding.show)
        else:
            bridge.allow_bluetooth()
            self.start_advertising()

    def start_advertising(self) -> None:
        """Si annuncia a MyWhoosh. La prima volta macOS chiede il permesso Rete locale."""
        if self.advertiser is None and obc.server.port:
            self.advertiser = obc.Advertiser(obc.server.port)

    def republish(self) -> None:
        if self.advertiser:
            self.advertiser.republish()

    def pressed(self, name: str, is_pressed: bool) -> None:
        """Tasto fisico premuto o rilasciato (thread del Bluetooth): lo si mostra nelle finestre."""
        self.settings.pressed(name, is_pressed)
        self.onboarding.pressed(name, is_pressed)

    def button_summary(self) -> list[dict]:
        from settings import button_summary
        return button_summary(self.settings.read_raw(), left="sinistro" in self.bridge.status)

    def batteries(self) -> dict[str, int]:
        """Batteria di ogni Click, solo se collegato: scollegato il numero sarebbe vecchio."""
        return {side: level for side, level in self.bridge.battery.items()
                if self.bridge.status.get(side) in CONNECTED_STATES}

    def status(self) -> dict:
        right = self.bridge.status.get("destro", "?")
        left = self.bridge.status.get("sinistro")  # None se il sinistro non si usa
        return {"click": friendly_status(right), "click_left": left and friendly_status(left),
                "batteries": self.batteries(), "mywhoosh": obc.server.connected}

    def refresh(self, _) -> None:
        batteries = self.batteries()
        for side, item in self.side_items.items():
            # Collegato: icona della batteria e percentuale. Altrimenti un pallino grigio.
            battery = batteries.get(side)
            item.title = f"Click {side}: {friendly_status(self.bridge.status.get(side, '?'))}" + (
                f" · {battery}%" if battery is not None else "")
            set_image(item, icons.battery(battery) if battery is not None else icons.gray_dot())

        self.last_item.title = f"Ultimo: {self.dispatcher.last_button}"

        button = getattr(getattr(self, "_nsapp", None), "nsstatusitem", None)
        if button is not None:
            button = button.button()
            any_connected = any(s in CONNECTED_STATES for s in self.bridge.status.values())
            button.setImage_(icons.logo())
            button.setTitle_("")
            button.setAppearsDisabled_(not any_connected)
        self.settings.push_status()
        self.onboarding.push_status()

    def quit(self, _) -> None:
        self.bridge.power_off_all("programma chiuso")
        rumps.quit_application()

    def toggle_login(self, item) -> None:
        if not login.available():
            rumps.alert("Avvia con il Mac", "Disponibile nell'app Click2Whoosh: quando la lanci da Terminale "
                                           "macOS non le darebbe il permesso Bluetooth.")
            return
        login.set_enabled(not login.is_enabled())
        item.state = login.is_enabled()

    def open_log(self, _) -> None:
        subprocess.Popen(["open", "-t", str(LOG_PATH)])

    def config_saved(self, changed: set) -> None:
        """Chiamata dalla finestra Impostazioni dopo aver scritto config.toml."""
        config = Config(CONFIG_PATH)  # se non è valida solleva e la finestra ripristina il file
        self.dispatcher.config = self.bridge.config = config
        if "use_left_side" in changed:
            notify("Click2Whoosh", "Questa modifica vale dal prossimo avvio del programma")


class SystemWatcher:
    """Reagisce agli eventi del sistema: stop e risveglio del Mac, MyWhoosh che si scollega dalla rete."""

    def __init__(self, bridge: ClickBridge, dispatcher: Dispatcher, republish):
        self.bridge = bridge
        self.dispatcher = dispatcher
        self.republish = republish  # rifà l'annuncio Bonjour
        self.lost_notified = False
        obc.server.on_change = self.mywhoosh_changed
        rumps.events.on_sleep.register(self.mac_sleeping)
        rumps.events.on_wake.register(self.mac_woke)

    def mywhoosh_changed(self, connected: bool) -> None:
        """MyWhoosh collegato o scollegato via rete (chiamata dal thread del Bluetooth)."""
        if not connected:
            # Se sei tu ad aver chiuso MyWhoosh non serve avvisare: si controlla tra qualche secondo.
            threading.Timer(4, self.check_lost).start()
        elif self.lost_notified:
            self.lost_notified = False
            notify("MyWhoosh ricollegato", "I comandi tornano via rete")

    def check_lost(self) -> None:
        if obc.server.connected or not mywhoosh_running():
            return
        self.lost_notified = True
        notify("MyWhoosh scollegato dalla rete",
               "Marce e sterzo passano dalla tastiera: tieni MyWhoosh in primo piano")

    def mac_sleeping(self) -> None:
        log.info("il Mac va in stop")
        # Il Click altrimenti resterebbe acceso fino a ~15 minuti; il flag evita che,
        # ricollegandosi al volo, gli si ridiano subito.
        self.bridge.mac_sleeping = True
        self.bridge.power_off_all("Mac in stop")

    def mac_woke(self) -> None:
        log.info("il Mac si è risvegliato")
        self.bridge.mac_sleeping = False
        self.dispatcher.last_press = time.monotonic()  # al risveglio si riparte come all'avvio
        self.republish()


class _Ticker(NSObject):
    def initWithCallback_(self, callback):
        self = objc.super(_Ticker, self).init()
        self.callback = callback
        return self

    def tick_(self, timer):
        self.callback()


def app_version() -> str:
    """Versione e data della build (dall'Info.plist dell'app), per capire quale copia è installata."""
    if login.app_bundle() is None:
        return "(da Terminale)"
    info = NSBundle.mainBundle().infoDictionary()
    build = str(info.get("CFBundleVersion", ""))  # AAAAMMGGhhmm, vedi Click2Whoosh.spec
    date = f" del {build[6:8]}/{build[4:6]}/{build[:4]}" if len(build) == 12 else ""
    return f"{info.get('CFBundleShortVersionString', '?')}{date}"


def set_image(item: rumps.MenuItem, image) -> None:
    item._menuitem.setImage_(image)


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console])


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(DEFAULT_CONFIG, CONFIG_PATH)
        log.info("creata la configurazione in %s", CONFIG_PATH)
    try:
        from settings import update_default_buttons
        update_default_buttons(CONFIG_PATH)
        return Config(CONFIG_PATH)
    except Exception as e:
        log.error("configurazione non valida (%s): uso quella predefinita", e)
        notify("Click2Whoosh", f"Configurazione non valida, uso quella predefinita: {e}")
        return Config(DEFAULT_CONFIG)


def main() -> None:
    setup_logging()
    # --senza-click: niente Bluetooth, per provare menu, impostazioni e rete senza toccare il Click.
    use_bluetooth = "--senza-click" not in sys.argv
    log.info("avvio Click2Whoosh%s", "" if use_bluetooth else " senza Bluetooth")
    first_run = not CONFIG_PATH.exists() or "--primo-avvio" in sys.argv  # apre la finestra di benvenuto
    config = load_config()
    dispatcher = Dispatcher(config)
    bridge = ClickBridge(dispatcher, config, use_bluetooth)
    bridge.start()
    MenuBarApp(bridge, dispatcher, first_run).run()


if __name__ == "__main__":
    main()
