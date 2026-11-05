"""Finestra di benvenuto (onboarding.html): permessi, collegamento di Click e MyWhoosh, cosa fanno tasti e menu.

Si apre da sola al primo avvio e poi dal menu ("Guida introduttiva…"). Al primo avvio
Bluetooth e annuncio sulla rete partono solo quando si preme "Consenti" (o alla chiusura
della finestra), così le richieste di macOS compaiono una alla volta, spiegate.
"""

import ctypes
import json
import subprocess
from pathlib import Path
from typing import Callable

import objc
from CoreBluetooth import CBManager, CBManagerAuthorizationAllowedAlways, CBManagerAuthorizationNotDetermined
from Foundation import NSDictionary, NSNetServiceBrowser, NSObject
from PyObjCTools import AppHelper

import login
import obc
from webwindow import WebWindow

HTML_PATH = Path(__file__).with_name("onboarding.html")
PRIVACY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_"
PRIVACY_PANES = {"bluetooth": "Bluetooth", "network": "LocalNetwork", "accessibility": "Accessibility"}


def bluetooth_permission() -> str:
    """"ok", "ask" (macOS non l'ha ancora chiesto) o "denied". Leggerlo non fa comparire la richiesta."""
    state = CBManager.authorization()
    if state == CBManagerAuthorizationAllowedAlways:
        return "ok"
    return "ask" if state == CBManagerAuthorizationNotDetermined else "denied"


# Accessibilità: AXIsProcessTrusted legge lo stato aggiornato. CGPreflightPostEventAccess no:
# ricorda la prima risposta finché l'app non si riavvia, e la spunta non diventava mai verde.
_ax = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
_ax.AXIsProcessTrusted.restype = ctypes.c_bool
_ax.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
_ax.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]


def accessibility_allowed() -> bool:
    return _ax.AXIsProcessTrusted()


def ask_accessibility() -> None:
    """macOS aggiunge Click2Whoosh all'elenco Accessibilità e propone di aprire le Impostazioni."""
    options = NSDictionary.dictionaryWithObject_forKey_(True, "AXTrustedCheckOptionPrompt")
    _ax.AXIsProcessTrustedWithOptions(objc.pyobjc_id(options))


class _NetworkProbe(NSObject):
    """Cerca il nostro annuncio Bonjour: se lo trova, il permesso Rete locale c'è.

    macOS non dice se il permesso è stato dato; senza, la ricerca semplicemente non trova niente.
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
        super().__init__("Benvenuto in Click2Whoosh", HTML_PATH, 600)
        self.get_status = get_status
        self.get_buttons = get_buttons
        self.start_bluetooth = start_bluetooth
        self.start_network = start_network
        self.advertising = advertising
        self.open_settings = open_settings
        self.probe = _NetworkProbe.alloc().init()

    def show(self) -> None:
        self.probe.start()  # se il permesso c'è già, la spunta compare subito
        super().show()
        self.push_status()

    def on_ready(self) -> None:
        self.call("c2wInit", {"buttons": self.get_buttons(), "login_available": login.available(),
                              "login_enabled": login.is_enabled()})
        self.push_status()

    def status(self) -> dict:
        return {**self.get_status(),
                "permissions": {"bluetooth": bluetooth_permission(),
                                # trovato l'annuncio, e non quello di un'altra copia di Click2Whoosh
                                "network": "ok" if self.probe.found and self.advertising() else "ask",
                                "accessibility": "ok" if accessibility_allowed() else "ask"}}

    def push_status(self) -> None:
        """Ogni secondo, dal thread principale."""
        if self.visible:
            self.call("c2wStatus", self.status())

    def pressed(self, name: str, is_pressed: bool) -> None:
        """Dal thread del Bluetooth: il tasto resta illuminato sul disegno finché è premuto."""
        def light() -> None:
            if self.visible:
                self._eval(f"window.c2wPressed && window.c2wPressed({json.dumps(name)}, {json.dumps(is_pressed)})")
        AppHelper.callAfter(light)

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
        # Chiusa prima di dare i permessi: si parte comunque, macOS li chiederà da solo.
        self.start_bluetooth()
        self.start_network()
