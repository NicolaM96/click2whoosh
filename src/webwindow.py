"""Finestra nativa con dentro una pagina HTML (WKWebView): la base di Impostazioni e Benvenuto.

La pagina parla con Python tramite window.webkit.messageHandlers.c2w.postMessage({type: ...});
Python chiama le funzioni window.c2w… della pagina. Due messaggi li gestisce questa classe:
"ready" (pagina caricata) e "size" (altezza del contenuto, per adattare la finestra).
Colori, disegno del Click e funzioni comuni stanno in common.html, incluso dalle pagine.
Le pagine sono template Jinja: il testo è scritto in inglese dentro {{ _("...") }} e viene
tradotto quando la pagina è generata (vedi i18n.py).
"""

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

import objc
from AppKit import (NSApp, NSBackingStoreBuffered, NSWindow, NSWindowStyleMaskClosable,
                    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskTitled, NSWindowWillCloseNotification)
from Foundation import NSMakeRect, NSNotificationCenter, NSObject
from PyObjCTools import AppHelper
from WebKit import WKWebView, WKWebViewConfiguration

import i18n

WKScriptMessageHandler = objc.protocolNamed("WKScriptMessageHandler")
HTML_DIR = Path(__file__).parent


def page_html(name: str) -> str:
    """La pagina nella lingua corrente, con dentro le parti comuni: autosufficiente, senza dipendere da percorsi su disco."""
    env = Environment(loader=FileSystemLoader(HTML_DIR), extensions=["jinja2.ext.i18n"],
                      autoescape=False, keep_trailing_newline=True)
    env.install_gettext_callables(i18n.gettext_, i18n.ngettext, newstyle=False)
    return env.get_template(name).render(lang=i18n.language)


class _MessageHandler(NSObject, protocols=[WKScriptMessageHandler]):
    def initWithCallback_(self, callback):
        self = objc.super(_MessageHandler, self).init()
        self.callback = callback
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        body = message.body()
        self.callback({str(k): body[k] for k in body})


class _WindowObserver(NSObject):
    """Finestra chiusa: lo riferisce alla WebWindow."""

    def initWithOwner_(self, owner):
        self = objc.super(_WindowObserver, self).init()
        self.owner = owner
        return self

    def closing_(self, notification):
        self.owner.closed()


class WebWindow:
    """Va creata e usata sul thread principale; call() si può chiamare da qualunque thread."""

    def __init__(self, title: str, page: str, width: float):
        self.title = title
        self.page = page
        self.width = width
        self.window = None
        self.webview = None
        self.handler = None
        self.observer = None
        self.ready = False  # la pagina ha finito di caricarsi: prima non le si manda niente

    @property
    def focused(self) -> bool:
        """In primo piano (finestra attiva). Letto anche dal thread del Bluetooth: si chiede
        a macOS ogni volta invece di contare sugli avvisi, che possono non arrivare."""
        return self.window is not None and bool(self.window.isKeyWindow())

    @property
    def visible(self) -> bool:
        return self.ready and self.window is not None and self.window.isVisible()

    def show(self) -> None:
        if self.window is None:
            self._create()
        # Da macOS 14 activateIgnoringOtherApps può essere ignorato: senza attivare l'app
        # la finestra non diventa attiva e i tasti del Click farebbero le loro azioni.
        if hasattr(NSApp, "activate"):
            NSApp.activate()
        else:
            NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def close(self) -> None:
        if self.window is not None:
            self.window.performClose_(None)

    def _create(self) -> None:
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.width, 380), style, NSBackingStoreBuffered, False)
        self.window.setTitle_(self.title)
        self.window.setReleasedWhenClosed_(False)
        self.window.center()
        self.observer = _WindowObserver.alloc().initWithOwner_(self)
        center = NSNotificationCenter.defaultCenter()
        center.addObserver_selector_name_object_(self.observer, "closing:", NSWindowWillCloseNotification, self.window)

        config = WKWebViewConfiguration.alloc().init()
        self.handler = _MessageHandler.alloc().initWithCallback_(self._message)
        config.userContentController().addScriptMessageHandler_name_(self.handler, "c2w")
        self.webview = WKWebView.alloc().initWithFrame_configuration_(self.window.contentView().bounds(), config)
        self.webview.setValue_forKey_(False, "drawsBackground")  # niente lampo bianco in modalità scura
        self.window.setContentView_(self.webview)
        self.webview.loadHTMLString_baseURL_(page_html(self.page), None)

    def fit_height(self, height: float) -> None:
        """Adatta l'altezza della finestra alla pagina, tenendo fermo il bordo in alto."""
        frame = self.window.frame()
        content = self.window.contentRectForFrameRect_(frame)
        new_height = max(300.0, min(height, 900.0))
        delta = new_height - content.size.height
        if abs(delta) < 1:
            return
        frame.origin.y -= delta
        frame.size.height += delta
        self.window.setFrame_display_animate_(frame, True, self.visible)

    def _eval(self, script: str) -> None:
        if self.webview is not None:
            self.webview.evaluateJavaScript_completionHandler_(script, None)

    def call(self, function: str, *args) -> None:
        """Chiama window.<function>(args) nella pagina, dal thread principale."""
        script = f"window.{function} && window.{function}({', '.join(json.dumps(a) for a in args)})"
        AppHelper.callAfter(self._eval, script)

    def _message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "ready":
            self.ready = True
            self.on_ready()
        elif kind == "size":
            self.fit_height(float(message["height"]))
        else:
            self.on_message(kind, message)

    # ---- da ridefinire

    def on_ready(self) -> None:
        pass

    def on_message(self, kind: str, message: dict) -> None:
        pass

    def closed(self) -> None:
        pass
