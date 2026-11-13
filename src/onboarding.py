"""Welcome window (onboarding.html): permissions, connecting the Click and MyWhoosh, what the buttons and menu do.

It opens by itself on first launch and then from the menu ("Getting Started…"). On first launch
Bluetooth and the network announcement start only when "Allow" is pressed (or when the
window is closed), so the macOS requests appear one at a time, explained.
"""

import ctypes
import subprocess
from typing import Callable

import objc
from CoreBluetooth import CBManager, CBManagerAuthorizationAllowedAlways, CBManagerAuthorizationNotDetermined
from Foundation import NSDictionary, NSNetServiceBrowser, NSObject

import login
import obc
from i18n import _
from webwindow import WebWindow

PAGE = "onboarding.html"
PRIVACY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_"
PRIVACY_PANES = {"bluetooth": "Bluetooth", "network": "LocalNetwork", "accessibility": "Accessibility"}


def bluetooth_permission() -> str:
    """"ok", "ask" (macOS has not asked yet) or "denied". Reading it does not make the request appear."""
    state = CBManager.authorization()
    if state == CBManagerAuthorizationAllowedAlways:
        return "ok"
    return "ask" if state == CBManagerAuthorizationNotDetermined else "denied"


# Accessibility: AXIsProcessTrusted reads the up-to-date state. CGPreflightPostEventAccess does not:
# it remembers the first answer until the app restarts, and the checkmark never turned green.
_ax = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
_ax.AXIsProcessTrusted.restype = ctypes.c_bool
_ax.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
_ax.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]


def accessibility_allowed() -> bool:
    return _ax.AXIsProcessTrusted()


def ask_accessibility() -> None:
    """macOS adds Click2Whoosh to the Accessibility list and offers to open the Settings."""
    options = NSDictionary.dictionaryWithObject_forKey_(True, "AXTrustedCheckOptionPrompt")
    _ax.AXIsProcessTrustedWithOptions(objc.pyobjc_id(options))


class _NetworkProbe(NSObject):
    """Looks for our Bonjour announcement: if it finds it, the Local Network permission is there.

    macOS does not say whether the permission was granted; without it, the search simply finds nothing.
    """

    def init(self):
        self = objc.super(_NetworkProbe, self).init()
        self.found = False
        self.browser = None
        return self

    def start(self) -> None:
        if self.browser is None:
            self.browser = NSNetServiceBrowser.alloc().init()
            self.browser.setDelegate_(self)
            self.browser.searchForServicesOfType_inDomain_(obc.SERVICE_TYPE, "local.")

    def netServiceBrowser_didFindService_moreComing_(self, browser, service, more):
        if service.name() == obc.ADVERTISED_NAME:
            self.found = True
            browser.stop()


class OnboardingWindow(WebWindow):
    def __init__(self, get_status: Callable[[], dict], get_buttons: Callable[[], list],
                 start_bluetooth: Callable[[], None], start_network: Callable[[], None],
                 advertising: Callable[[], bool], open_settings: Callable[[], None]):
        super().__init__(_("Welcome to Click2Whoosh"), PAGE, 600)
        self.get_status = get_status
        self.get_buttons = get_buttons
        self.start_bluetooth = start_bluetooth
        self.start_network = start_network
        self.advertising = advertising
        self.open_settings = open_settings
        self.probe = _NetworkProbe.alloc().init()

    def show(self) -> None:
        self.probe.start()  # if the permission is already there, the checkmark shows right away
        super().show()
        self.push_status()

    def on_ready(self) -> None:
        self.call("c2wInit", {"buttons": self.get_buttons(), "login_available": login.available(),
                              "login_enabled": login.is_enabled()})
        self.push_status()

    def status(self) -> dict:
        return {**self.get_status(),
                "permissions": {"bluetooth": bluetooth_permission(),
                                # announcement found, and not the one of another copy of Click2Whoosh
                                "network": "ok" if self.probe.found and self.advertising() else "ask",
                                "accessibility": "ok" if accessibility_allowed() else "ask"}}

    def push_status(self) -> None:
        """Every second, from the main thread."""
        if self.visible:
            self.call("c2wStatus", self.status())

    def pressed(self, name: str, is_pressed: bool) -> None:
        """From the Bluetooth thread: the button stays lit on the drawing while it is pressed."""
        self.call("c2wPressed", name, is_pressed)

    def on_message(self, kind: str, message: dict) -> None:
        if kind == "allow":
            what = message["what"]
            if what == "bluetooth":
                self.start_bluetooth()
            elif what == "network":
                self.start_network()
                self.probe.start()
            elif what == "accessibility":
                ask_accessibility()
        elif kind == "open_privacy":
            subprocess.Popen(["open", PRIVACY_URL + PRIVACY_PANES[message["what"]]])
        elif kind == "login":
            login.set_enabled(bool(message["enabled"]))
        elif kind == "open_settings":
            self.close()
            self.open_settings()
        elif kind == "finish":
            self.close()

    def closed(self) -> None:
        # Closed before granting the permissions: we start anyway, macOS will ask by itself.
        self.start_bluetooth()
        self.start_network()
