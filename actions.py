"""Azioni che un tasto del Click può fare sul Mac.

Formati accettati in config.toml:
    "obc:shift_up"     comando OpenBikeControl mandato a MyWhoosh via rete (vedi obc.BUTTONS);
                       marce e sterzo, se MyWhoosh non è collegato, ripiegano sulla tastiera
    "obc:steer_left+left"  più comandi nello stesso messaggio: MyWhoosh usa quello che gli serve
    "key:k"            preme e rilascia subito un tasto (cambio marcia, emote)
    "hold:a"           tiene premuto il tasto finché tieni premuto il pulsante (sterzo)
    "media:playpause"  tasti multimediali: playpause, next, previous, volup, voldown
    "screenshot"       salva uno screenshot sulla Scrivania
    "none"             niente

"key" e "hold" partono solo se l'app scelta (MyWhoosh) è in primo piano,
così il Click non scrive lettere in altre finestre. Media, screenshot e i
comandi OpenBikeControl partono sempre.
"""

import datetime
import logging
import subprocess
from pathlib import Path

import Quartz
from AppKit import NSEvent, NSSound, NSWorkspace
from PyObjCTools import AppHelper

import obc

log = logging.getLogger("click2whoosh")

# Se MyWhoosh non è collegato via OpenBikeControl, questi comandi usano la tastiera.
OBC_KEYBOARD_FALLBACK = {"shift_up": "key:k", "shift_down": "key:i", "steer_left": "hold:a", "steer_right": "hold:d"}

# Codici tasto macOS (layout fisico, indipendenti dalla lingua della tastiera).
KEY_CODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    "return": 36, "tab": 48, "space": 49, "escape": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
}

# Codici NX_KEYTYPE_* dei tasti multimediali.
MEDIA_KEYS = {"volup": 0, "voldown": 1, "playpause": 16, "next": 17, "previous": 18}

# Le emote di MyWhoosh, sui tasti da 1 a 7.
EMOTES = ["pace", "saluto", "pugno", "dab", "gomitata", "brindisi", "pollice su"]

# Nomi leggibili, per il menu e la finestra impostazioni: (gruppo, azione, etichetta).
# Solo quello che MyWhoosh esegue davvero: visuale, menu, inversione a U, ERG ecc. li
# dichiara via OpenBikeControl ma li ignora (lo segnala anche BikeControl).
ACTION_CHOICES = [
    ("MyWhoosh", "obc:shift_up", "Marcia su"),
    ("MyWhoosh", "obc:shift_down", "Marcia giù"),
    ("MyWhoosh", "obc:steer_left", "Sterza a sinistra"),
    ("MyWhoosh", "obc:steer_right", "Sterza a destra"),
    # Sinistra/destra insieme allo sterzo: da provare per scegliere la strada ai bivi.
    ("MyWhoosh", "obc:steer_left+left", "Sterza a sinistra + Sinistra (bivi, da provare)"),
    ("MyWhoosh", "obc:steer_right+right", "Sterza a destra + Destra (bivi, da provare)"),
    ("Musica", "media:playpause", "Play/pausa"),
    ("Musica", "media:next", "Brano successivo"),
    ("Musica", "media:previous", "Brano precedente"),
    ("Musica", "media:volup", "Volume su"),
    ("Musica", "media:voldown", "Volume giù"),
    ("Mac", "screenshot", "Screenshot del Mac"),
    ("Tastiera", "key:u", "Tasto U (interfaccia minimale)"),
    *(("Tastiera", f"key:{n}", f"Emote {n}: {name}") for n, name in enumerate(EMOTES, start=1)),
]
ACTION_LABELS = {spec: label for _, spec, label in ACTION_CHOICES}
# Azioni che restano attive finché tieni premuto: niente doppio clic né pressione lunga.
HELD_ACTIONS = {"obc:steer_left", "obc:steer_right", "obc:steer_left+left", "obc:steer_right+right"}
# Tasti di default, secondo i Click in uso (chiave: si usa anche il sinistro).
# Col solo destro fa tutto lui; con tutti e due, come in Zwift: − e + marce, frecce sterzo.
# "repeat": tasti che, tenuti premuti, ripetono il clic (es. più cambi di marcia di fila).
DEFAULT_BUTTONS = {
    False: {"buttons": {"plus": "obc:shift_up", "b": "obc:shift_down", "z": "obc:steer_left",
                        "a": "obc:steer_right", "y": "media:playpause", "minus": "obc:shift_down",
                        "nav_up": "obc:shift_up", "nav_down": "obc:shift_down",
                        "nav_left": "obc:steer_left", "nav_right": "obc:steer_right"},
            "repeat": ["plus", "b", "minus"]},
    True: {"buttons": {"plus": "obc:shift_up", "minus": "obc:shift_down",
                       "nav_left": "obc:steer_left", "nav_right": "obc:steer_right",
                       "nav_up": "media:volup", "nav_down": "media:voldown",
                       "y": "media:playpause", "z": "key:u", "a": "key:2", "b": "screenshot"},
           "repeat": ["plus", "minus", "nav_up", "nav_down"]},
}
DEFAULT_REPEAT_BUTTONS = DEFAULT_BUTTONS[True]["repeat"]


def repeatable(spec: str) -> bool:
    """Se l'azione ha senso ripeterla tenendo premuto il tasto."""
    if spec in ("media:volup", "media:voldown"):
        return True
    return spec.startswith(("obc:", "key:")) and spec not in HELD_ACTIONS


def describe(spec: str) -> str:
    if spec in ACTION_LABELS:
        return ACTION_LABELS[spec]
    kind, _, arg = spec.partition(":")
    if kind == "key":
        return f"Tasto {arg.upper()}"
    if kind == "hold":
        return f"Tieni premuto {arg.upper()}"
    return "Niente" if spec == "none" else spec


def frontmost_app() -> str:
    """Nome dell'app in primo piano (es. "MyWhoosh Indoor Cycling App")."""
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return app.localizedName() or "" if app else ""


def post_key(code: int, down: bool) -> None:
    event = Quartz.CGEventCreateKeyboardEvent(None, code, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def post_media_key(key: int) -> None:
    for down in (True, False):
        flags = 0xA00 if down else 0xB00
        event = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), flags, 0, 0, 0, 8, (key << 16) | flags, -1
        )
        Quartz.CGEventPost(0, event.CGEvent())


# Suoni di sistema per confermare il cambio: il Click v2 non vibra.
SHIFT_SOUNDS = {"up": "Tink", "down": "Pop"}
SHIFT_VOLUME = 0.35
_sounds: dict = {}


def play_shift_sound(direction: str) -> None:
    """Suona sul thread principale (NSSound non va usato da altri thread)."""
    def play() -> None:
        sound = _sounds.get(direction)
        if sound is None:
            sound = _sounds[direction] = NSSound.soundNamed_(SHIFT_SOUNDS[direction])
            if sound is not None:
                sound.setVolume_(SHIFT_VOLUME)
        if sound is not None:
            sound.stop()  # con i cambi ripetuti il suono precedente può essere ancora in corso
            sound.play()
    AppHelper.callAfter(play)


def take_screenshot() -> None:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H.%M.%S")
    path = Path.home() / "Desktop" / f"MyWhoosh {stamp}.png"
    subprocess.Popen(["screencapture", "-x", str(path)])
    log.info("screenshot salvato in %s", path)


class Action:
    """Un'azione letta da config.toml."""

    def __init__(self, spec: str):
        self.spec = spec
        kind, _, arg = spec.partition(":")
        self.kind, self.arg = kind, arg
        if kind in ("key", "hold") and arg not in KEY_CODES:
            raise ValueError(f"tasto sconosciuto in '{spec}'. Disponibili: {', '.join(KEY_CODES)}")
        if kind == "media" and arg not in MEDIA_KEYS:
            raise ValueError(f"tasto multimediale sconosciuto in '{spec}'. Disponibili: {', '.join(MEDIA_KEYS)}")
        commands = arg.split("+") if kind == "obc" else []
        for command in commands:
            if command not in obc.BUTTONS:
                raise ValueError(f"comando sconosciuto in '{spec}'. Disponibili: {', '.join(obc.BUTTONS)}")
        self.button_ids = [obc.BUTTONS[c] for c in commands]
        if kind not in ("obc", "key", "hold", "media", "screenshot", "none"):
            raise ValueError(f"azione sconosciuta: '{spec}'")
        # Senza rete vale il primo comando: "shift_up+select" diventa il tasto K.
        first = commands[0] if commands else None
        self.fallback = Action(OBC_KEYBOARD_FALLBACK[first]) if first in OBC_KEYBOARD_FALLBACK else None
        self.via_fallback = False

    @property
    def needs_target_app(self) -> bool:
        if self.kind == "obc":
            return self.fallback is not None and not obc.server.connected
        return self.kind in ("key", "hold")

    @property
    def shift_direction(self) -> str | None:
        """"up"/"down" se l'azione cambia marcia (anche via tastiera di riserva), altrimenti None."""
        if self.kind == "obc" and self.button_ids:
            return {obc.BUTTONS["shift_up"]: "up", obc.BUTTONS["shift_down"]: "down"}.get(self.button_ids[0])
        if self.kind == "key":
            return {"k": "up", "i": "down"}.get(self.arg)
        return None

    @property
    def stays_pressed(self) -> bool:
        """Va rilasciata quando si rilascia il pulsante."""
        return self.kind in ("hold", "obc")

    def press(self) -> None:
        if self.kind == "obc":
            self.via_fallback = not obc.server.connected and self.fallback is not None
            if self.via_fallback:
                self.fallback.press()
            else:
                obc.server.send(self.button_ids, True)
        elif self.kind == "key":
            post_key(KEY_CODES[self.arg], True)
            post_key(KEY_CODES[self.arg], False)
        elif self.kind == "hold":
            post_key(KEY_CODES[self.arg], True)
        elif self.kind == "media":
            post_media_key(MEDIA_KEYS[self.arg])
        elif self.kind == "screenshot":
            take_screenshot()

    def release(self) -> None:
        if self.kind == "obc":
            if self.via_fallback:
                self.fallback.release()
            else:
                obc.server.send(self.button_ids, False)
        elif self.kind == "hold":
            post_key(KEY_CODES[self.arg], False)

    def __str__(self) -> str:
        return self.spec
