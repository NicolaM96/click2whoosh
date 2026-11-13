"""click2whoosh: use the Zwift Click v2 with MyWhoosh on a Mac.

Launch:
    .venv/bin/python src/app.py

An icon appears in the menu bar. Bluetooth runs in a separate thread;
the menu bar reads the status once a second.

The configuration lives in ~/Library/Application Support/Click2Whoosh/config.toml
(created on first launch by copying config.toml from this folder), the log in
~/Library/Logs/Click2Whoosh/click2whoosh.log.
"""

import asyncio
import logging
import shutil
import subprocess
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
from ble import (BUTTON_LABELS, SIDE_LEFT, SIDE_RIGHT, STATUS_BLUETOOTH_DENIED, STATUS_BLUETOOTH_OFF,
                 STATUS_BLUETOOTH_UNAVAILABLE, STATUS_CONNECTED, STATUS_DISCONNECTED, STATUS_RESTARTING,
                 STATUS_SEARCHING, STATUS_UNUSED, TESTED_FIRMWARE, ClickEvents, ClickSide)
from i18n import _

DEFAULT_CONFIG = Path(__file__).with_name("config.toml")
CONFIG_PATH = Path.home() / "Library" / "Application Support" / "Click2Whoosh" / "config.toml"
LOG_PATH = Path.home() / "Library" / "Logs" / "Click2Whoosh" / "click2whoosh.log"
TARGET_APP = "MyWhoosh"  # it is enough for the app name to contain it ("MyWhoosh Indoor Cycling App")
# The left Click is restarted as soon as it reports its lock (about 51 s after starting, see ble.lock_state);
# this is only the fallback if that message does not arrive (e.g. another firmware), so it is later than that.
LEFT_RESET_S = 60
WAKE_INTERVAL_S = 300 # each "wake" sequence keeps the Click on for ~15 minutes: it is renewed every 5
SLEEP_AFTER_FRONT_MIN = 60  # no buttons, with MyWhoosh in front (you may be riding in ERG)
SLEEP_AFTER_MIN = 10  # no buttons, with MyWhoosh behind other windows
REPEAT_DELAY_S = 0.4  # holding a repeatable button, how long before the repetition starts
REPEAT_INTERVAL_S = 0.2
CONNECTED_STATES = (STATUS_CONNECTED, STATUS_RESTARTING)
WAITING_STATES = (STATUS_SEARCHING, STATUS_DISCONNECTED)
STATUS_WAITING = "waiting"  # searching and disconnected look the same to the user
log = logging.getLogger("click2whoosh")


def simple_status(status: str) -> str:
    """The Click's status reduced to connected / waiting / a problem, without the
    searching/disconnected seesaw while it sleeps. It is a code: status_text() gives the text."""
    if status in CONNECTED_STATES:
        return STATUS_CONNECTED
    if status in WAITING_STATES:
        return STATUS_WAITING
    return status


def status_text(code: str) -> str:
    """The status from simple_status() in words, in the user's language."""
    return {
        STATUS_CONNECTED: _("connected"),
        STATUS_WAITING: _("waiting (press a button)"),
        STATUS_BLUETOOTH_OFF: _("Bluetooth off"),
        STATUS_BLUETOOTH_DENIED: _("Bluetooth permission missing"),
        STATUS_BLUETOOTH_UNAVAILABLE: _("Bluetooth unavailable"),
        STATUS_UNUSED: _("Bluetooth not used"),
    }.get(code, code)


def click_name(side: str) -> str:
    return _("Left Click") if side == "left" else _("Right Click")


def notify(title: str, message: str) -> None:
    """macOS notification. In the packaged app it goes through UNUserNotificationCenter, so the
    Click2Whoosh logo shows; from Terminal there is no app to attribute it to and osascript is used
    (rumps.notification uses NSUserNotification, which no longer arrives on recent macOS)."""
    if login.app_bundle() is None:
        notify_with_osascript(title, message)
        return
    from UserNotifications import (UNAuthorizationOptionAlert, UNAuthorizationOptionSound, UNMutableNotificationContent,
                                   UNNotificationRequest, UNUserNotificationCenter)
    center = UNUserNotificationCenter.currentNotificationCenter()
    if center.delegate() is None:
        center.setDelegate_(_NotificationDelegate.alloc().init())

    def authorized(granted: bool, error) -> None:
        # The first time macOS asks for permission; after that it answers right away.
        if error is not None:
            log.info("system notifications not available (%s): using osascript", error)
            notify_with_osascript(title, message)
        elif not granted:
            log.info("notifications not allowed: %s", message)
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
    """Shows the notification even when Click2Whoosh is in front (e.g. with Settings open)."""

    def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, center, notification, handler):
        from UserNotifications import UNNotificationPresentationOptionBanner, UNNotificationPresentationOptionList
        handler(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList)


def mywhoosh_running() -> bool:
    return any(TARGET_APP.lower() in (app.localizedName() or "").lower()
               for app in NSWorkspace.sharedWorkspace().runningApplications())


def mywhoosh_in_front() -> bool:
    return TARGET_APP.lower() in frontmost_app().lower()


class Config:
    """config.toml: only what makes sense to change (the same things as the Settings window)."""

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
                raise ValueError(_("{key} must be between {low} and {high}").format(key=key, low=low, high=high))
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
    """Turns button presses and releases into actions on the Mac."""

    def __init__(self, config: Config):
        self.config = config
        self.last_button = "—"
        self.last_press = time.monotonic()  # at startup it counts as activity
        self.active: dict[str, Action] = {}  # "hold" actions pressed, to be released
        self.long_timers: dict[str, threading.Timer] = {}
        self.long_fired: set[str] = set()
        self.single_timers: dict[str, threading.Timer] = {}
        self.double_fired: set[str] = set()
        self.repeats: dict[str, threading.Event] = {}  # buttons that are repeating; set() stops them
        self.listener = None  # called on every physical press/release: (name, pressed)
        # If True the buttons run no actions (Settings window in front: they are for selecting).
        self.capture = lambda: False
        self.captured: set[str] = set()

    def allowed(self, action: Action) -> bool:
        if action.needs_target_app and not mywhoosh_in_front():
            log.info("ignoring %s: '%s' is in front, not %s", action, frontmost_app(), TARGET_APP)
            return False
        return True

    def run(self, name: str, action: Action) -> None:
        button, _sep, how = name.partition(" ")
        how_text = {"double": _("double click"), "long": _("long press")}.get(how, how)
        label = BUTTON_LABELS.get(button, button) + (f" {how_text}" if how else "")
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
        """Presses and releases right away (actions decided on release or after a wait)."""
        self.run(name, action)
        self.release_active(name)

    def on_press(self, name: str) -> None:
        self.last_press = time.monotonic()
        if self.listener:
            self.listener(name, True)
        if self.capture():
            self.captured.add(name)
            self.last_button = _("{button} → highlight only (window in front)").format(button=BUTTON_LABELS.get(name, name))
            log.info(self.last_button)
            return
        waiting = self.single_timers.pop(name, None)
        if waiting:
            # Second click within the time: double click.
            waiting.cancel()
            self.double_fired.add(name)
            self.tap(f"{name} double", self.config.double_press[name])
            return
        if name in self.config.long_press:
            self.long_fired.discard(name)
            timer = threading.Timer(self.config.long_press_s, self.fire_long, args=(name,))
            self.long_timers[name] = timer
            timer.start()
            return
        if name in self.config.double_press:
            return  # decided on release
        if name not in self.config.buttons:
            self.last_button = _("{button} → nothing").format(button=BUTTON_LABELS.get(name, name))
            return
        action = self.config.buttons[name]
        self.run(name, action)
        if name in self.config.repeat and repeatable(action.spec):
            stop = threading.Event()
            self.repeats[name] = stop
            threading.Thread(target=self.repeat, args=(name, action, stop), daemon=True).start()

    def repeat(self, name: str, action: Action, stop: threading.Event) -> None:
        """While the button is held, the action repeats (e.g. several shifts in a row)."""
        if stop.wait(REPEAT_DELAY_S):
            return
        while not stop.is_set():
            if action.stays_pressed:
                action.release()  # each repetition must be a new press
            self.run(name, action)
            stop.wait(REPEAT_INTERVAL_S)

    def fire_long(self, name: str) -> None:
        self.long_fired.add(name)
        self.tap(f"{name} long", self.config.long_press[name])

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
            # Wait for a possible second click before deciding it was a single click.
            single = self.config.buttons.get(name)
            timer = threading.Timer(self.config.double_window_s, self.fire_single, args=(name, single))
            self.single_timers[name] = timer
            timer.start()
        elif timer and name in self.config.buttons:
            # Short press on a button with a long press: normal action, like a tap.
            self.tap(name, self.config.buttons[name])
        self.release_active(name)

    def fire_single(self, name: str, action: Action | None) -> None:
        self.single_timers.pop(name, None)
        if action:
            self.tap(name, action)

    def release_active(self, name: str) -> None:
        # Always release, even if MyWhoosh is no longer in front in the meantime,
        # otherwise the key would stay pressed.
        action = self.active.pop(name, None)
        if action:
            action.release()


class ClickBridge:
    """Thread with the asyncio loop that keeps the Clicks and the OpenBikeControl server connected."""

    def __init__(self, dispatcher: Dispatcher, config: Config, use_bluetooth: bool = True):
        self.dispatcher = dispatcher
        self.config = config
        self.use_bluetooth = use_bluetooth
        self.status: dict[str, str] = {"right": STATUS_SEARCHING if use_bluetooth else STATUS_UNUSED}
        if config.use_left_side:
            self.status["left"] = STATUS_SEARCHING
        self.battery: dict[str, int] = {}
        self.warned: set[str] = set()  # low battery already reported
        self.warned_firmware: set[str] = set()
        self.mac_sleeping = False  # while the Mac sleeps the Click is not kept awake
        self.last_reason = ""  # to write the reason in the log only when it changes
        self.sides: list[ClickSide] = []
        self.loop: asyncio.AbstractEventLoop | None = None  # the Bluetooth loop, once it starts
        # On first launch the search for the Click (and so the Bluetooth permission request)
        # waits for the "Allow" button of the welcome window.
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
            notify("Zwift Click", _("{click} battery at {level}%").format(click=click_name(side), level=level))

    def on_firmware(self, side: str, version: str) -> None:
        if version != TESTED_FIRMWARE and side not in self.warned_firmware:
            self.warned_firmware.add(side)
            notify(_("Click firmware differs from the tested one"),
                   _("{click}: firmware {version}, tested only with {tested}. If something doesn't work, "
                     "report it on GitHub with the log attached (menu → Open Log).").format(
                       click=click_name(side), version=version, tested=TESTED_FIRMWARE))

    def search_often(self) -> bool:
        """Continuous search with MyWhoosh open; otherwise light (the Mac uses less)."""
        return not self.mac_sleeping and mywhoosh_running()

    def keep_awake_allowed(self) -> bool:
        """Keeping it awake drains the battery: only with MyWhoosh open and buttons pressed recently.

        With MyWhoosh in front you are probably riding (maybe in ERG, without
        shifting), so we wait longer before letting it switch off.
        """
        reason = self.sleep_reason()
        if reason and reason != self.last_reason:
            log.info("no need to keep it awake: %s", reason)
        self.last_reason = reason
        return not reason

    def sleep_reason(self) -> str:
        """Why the Click may switch off; empty string if it should be kept awake."""
        if self.mac_sleeping:
            return "the Mac is going to sleep"
        idle_min = (time.monotonic() - self.dispatcher.last_press) / 60
        if idle_min < 2:
            return ""  # you are using it right now (maybe only for the music)
        if not mywhoosh_running():
            return f"{TARGET_APP} is not open and no button for at least 2 minutes"
        in_front = mywhoosh_in_front()
        limit = SLEEP_AFTER_FRONT_MIN if in_front else SLEEP_AFTER_MIN
        if idle_min >= limit:
            where = "in front" if in_front else "in the background"
            return f"no button for {limit} minutes with {TARGET_APP} {where}"
        return ""

    def power_off_all(self, reason: str) -> None:
        """Switches off the connected Clicks; to be called from a thread other than the Bluetooth one."""
        if self.loop is None:
            return

        async def power_off() -> None:
            # gather must be called inside the Bluetooth loop, not from the calling thread.
            await asyncio.gather(*(side.power_off(reason) for side in self.sides))

        future = asyncio.run_coroutine_threadsafe(power_off(), self.loop)
        try:
            future.result(timeout=2)
        except Exception as e:
            log.info("switching off at quit failed: %s", e)

    def start(self) -> None:
        """Starts the thread; returns when the OpenBikeControl server (if active) has its port."""
        self.obc_ready = threading.Event()
        threading.Thread(target=self.run, daemon=True).start()
        self.obc_ready.wait(5)

    def allow_bluetooth(self) -> None:
        self.bluetooth_go.set()

    def run(self) -> None:
        sides = [SIDE_RIGHT, SIDE_LEFT] if self.config.use_left_side else [SIDE_RIGHT]
        # The right Click stays up with the "wake" sequence. The left one stops sending buttons about
        # a minute after it starts (it wants Zwift's unlock): like BikeControl we restart it before that.
        self.sides = [ClickSide(side, self.events, keepalive="reset", keepalive_interval=LEFT_RESET_S)
                      if side == SIDE_LEFT else
                      ClickSide(side, self.events, keepalive="wake", keepalive_interval=WAKE_INTERVAL_S)
                      for side in sides] if self.use_bluetooth else []

        async def main() -> None:
            self.loop = asyncio.get_running_loop()
            try:
                await obc.server.start()
            except OSError as e:
                log.error("OpenBikeControl not started: %s", e)
            self.obc_ready.set()
            await asyncio.to_thread(self.bluetooth_go.wait)
            await asyncio.gather(*(s.run_forever() for s in self.sides), asyncio.Event().wait())

        asyncio.run(main())


class MenuBarApp(rumps.App):
    def __init__(self, bridge: ClickBridge, dispatcher: Dispatcher, first_run: bool = False):
        # "Quit" is our own item: the rumps one would close without switching the Click off.
        super().__init__("Click2Whoosh", quit_button=None)
        self.bridge = bridge
        self.dispatcher = dispatcher
        self.advertiser = None  # Bonjour announcement for MyWhoosh: see start_advertising()

        from onboarding import OnboardingWindow
        from settings import SettingsWindow
        self.settings = SettingsWindow(CONFIG_PATH, self.status, self.config_saved, lambda: self.dispatcher.config)
        self.onboarding = OnboardingWindow(self.status, self.button_summary, bridge.allow_bluetooth,
                                           self.start_advertising, lambda: self.advertiser is not None,
                                           self.settings.show)
        dispatcher.listener = self.pressed
        # With one of the two windows in front the Click's buttons only light up.
        dispatcher.capture = lambda: self.settings.focused or self.onboarding.focused
        self.watcher = SystemWatcher(bridge, dispatcher, self.republish)

        self.header = rumps.MenuItem(f"Click2Whoosh {app_version()}")
        self.side_items = {side: rumps.MenuItem("") for side in bridge.status}
        self.last_item = rumps.MenuItem("")
        settings_item = rumps.MenuItem(_("Settings…"), callback=lambda sender: self.settings.show(), key=",")
        guide_item = rumps.MenuItem(_("Getting Started…"), callback=lambda sender: self.onboarding.show())
        log_item = rumps.MenuItem(_("Open Log"), callback=self.open_log)
        self.login_item = rumps.MenuItem(_("Launch at Login"), callback=self.toggle_login)
        self.menu = [
            self.header, *self.side_items.values(), self.last_item, None,
            settings_item, guide_item, log_item, None,
            self.login_item, None,
            rumps.MenuItem(_("Quit"), callback=self.quit, key="q"),
        ]
        set_image(settings_item, icons.symbol("slider.horizontal.3"))
        set_image(guide_item, icons.symbol("questionmark.circle"))
        set_image(log_item, icons.symbol("doc.text"))
        set_image(self.header, icons.logo(14))
        for item in (self.header, self.last_item):
            item._menuitem.setEnabled_(False)  # informational rows, not clickable
        self.login_item.state = login.is_enabled()
        self.refresh(None)
        # rumps timers stop while the menu is open: this one runs in all the run loop
        # modes, so status and battery update even with the menu open.
        self.ticker = _Ticker.alloc().initWithCallback_(lambda: self.refresh(None))
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self.ticker, "tick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        if first_run:
            # Bluetooth and network start from the window's buttons, so macOS asks for one permission
            # at a time. After the app starts: if the window is created before, WebKit does not load the page.
            AppHelper.callLater(1.0, self.onboarding.show)
        else:
            bridge.allow_bluetooth()
            self.start_advertising()

    def start_advertising(self) -> None:
        """Announces itself to MyWhoosh. The first time macOS asks for the Local Network permission."""
        if self.advertiser is None and obc.server.port:
            self.advertiser = obc.Advertiser(obc.server.port)

    def republish(self) -> None:
        if self.advertiser:
            self.advertiser.republish()

    def pressed(self, name: str, is_pressed: bool) -> None:
        """Physical button pressed or released (Bluetooth thread): shown in the windows."""
        self.settings.pressed(name, is_pressed)
        self.onboarding.pressed(name, is_pressed)

    def button_summary(self) -> list[dict]:
        from settings import button_summary
        return button_summary(self.settings.read_raw(), left="left" in self.bridge.status)

    def batteries(self) -> dict[str, int]:
        """Battery of each Click, only if connected: disconnected the number would be stale."""
        return {side: level for side, level in self.bridge.battery.items()
                if self.bridge.status.get(side) in CONNECTED_STATES}

    def status(self) -> dict:
        right = self.bridge.status.get("right", "?")
        left = self.bridge.status.get("left")  # None if the left Click is not used
        return {"click": simple_status(right), "click_left": left and simple_status(left),
                "batteries": self.batteries(), "mywhoosh": obc.server.connected}

    def refresh(self, sender) -> None:
        batteries = self.batteries()
        for side, item in self.side_items.items():
            # Connected: battery icon and percentage. Otherwise a gray dot.
            battery = batteries.get(side)
            item.title = _("{click}: {status}").format(
                click=click_name(side), status=status_text(simple_status(self.bridge.status.get(side, "?")))) + (
                f" · {battery}%" if battery is not None else "")
            set_image(item, icons.battery(battery) if battery is not None else icons.gray_dot())

        self.last_item.title = _("Last: {button}").format(button=self.dispatcher.last_button)

        button = getattr(getattr(self, "_nsapp", None), "nsstatusitem", None)
        if button is not None:
            button = button.button()
            any_connected = any(s in CONNECTED_STATES for s in self.bridge.status.values())
            button.setImage_(icons.logo())
            button.setTitle_("")
            button.setAppearsDisabled_(not any_connected)
        self.settings.push_status()
        self.onboarding.push_status()

    def quit(self, sender) -> None:
        self.bridge.power_off_all("app closed")
        rumps.quit_application()

    def toggle_login(self, item) -> None:
        if not login.available():
            rumps.alert(_("Launch at Login"), _("Only available in the Click2Whoosh app: launched from Terminal, "
                                                "macOS would not give it the Bluetooth permission."))
            return
        login.set_enabled(not login.is_enabled())
        item.state = login.is_enabled()

    def open_log(self, sender) -> None:
        subprocess.Popen(["open", "-t", str(LOG_PATH)])

    def config_saved(self, changed: set) -> None:
        """Called by the Settings window after writing config.toml."""
        config = Config(CONFIG_PATH)  # if invalid it raises and the window restores the file
        self.dispatcher.config = self.bridge.config = config
        if "use_left_side" in changed:
            notify("Click2Whoosh", _("This change takes effect the next time the app starts"))


class SystemWatcher:
    """Reacts to system events: the Mac sleeping and waking, MyWhoosh disconnecting from the network."""

    def __init__(self, bridge: ClickBridge, dispatcher: Dispatcher, republish):
        self.bridge = bridge
        self.dispatcher = dispatcher
        self.republish = republish  # renews the Bonjour announcement
        self.lost_notified = False
        obc.server.on_change = self.mywhoosh_changed
        rumps.events.on_sleep.register(self.mac_sleeping)
        rumps.events.on_wake.register(self.mac_woke)

    def mywhoosh_changed(self, connected: bool) -> None:
        """MyWhoosh connected or disconnected over the network (called from the Bluetooth thread)."""
        if not connected:
            # If you closed MyWhoosh yourself there is no need to warn: we check again in a few seconds.
            threading.Timer(4, self.check_lost).start()
        elif self.lost_notified:
            self.lost_notified = False
            notify(_("MyWhoosh reconnected"), _("Commands go over the network again"))

    def check_lost(self) -> None:
        if obc.server.connected or not mywhoosh_running():
            return
        self.lost_notified = True
        notify(_("MyWhoosh disconnected from the network"),
               _("Gears and steering now go through the keyboard: keep MyWhoosh in front"))

    def mac_sleeping(self) -> None:
        log.info("the Mac is going to sleep")
        # Otherwise the Click would stay on for up to ~15 minutes; the flag stops it from
        # being given the wake again right away when it reconnects quickly.
        self.bridge.mac_sleeping = True
        self.bridge.power_off_all("Mac asleep")

    def mac_woke(self) -> None:
        log.info("the Mac woke up")
        self.bridge.mac_sleeping = False
        self.dispatcher.last_press = time.monotonic()  # on wake we start again as at launch
        self.republish()


class _Ticker(NSObject):
    def initWithCallback_(self, callback):
        self = objc.super(_Ticker, self).init()
        self.callback = callback
        return self

    def tick_(self, timer):
        self.callback()


def app_version() -> str:
    """Version and build date (from the app's Info.plist), to tell which copy is installed."""
    if login.app_bundle() is None:
        return _("(from Terminal)")
    info = NSBundle.mainBundle().infoDictionary()
    build = str(info.get("CFBundleVersion", ""))  # YYYYMMDDhhmm, see Click2Whoosh.spec
    date = f" ({build[:4]}-{build[4:6]}-{build[6:8]})" if len(build) == 12 else ""
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
        log.info("configuration created in %s", CONFIG_PATH)
    try:
        from settings import update_default_buttons
        update_default_buttons(CONFIG_PATH)
        return Config(CONFIG_PATH)
    except Exception as e:
        log.error("invalid configuration (%s): using the default", e)
        notify("Click2Whoosh", _("Invalid configuration, using the default one: {error}").format(error=e))
        return Config(DEFAULT_CONFIG)


def main() -> None:
    setup_logging()
    log.info("starting Click2Whoosh")
    first_run = not CONFIG_PATH.exists()  # opens the welcome window
    config = load_config()
    dispatcher = Dispatcher(config)
    bridge = ClickBridge(dispatcher, config)
    bridge.start()
    MenuBarApp(bridge, dispatcher, first_run).run()


if __name__ == "__main__":
    main()
