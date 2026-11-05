"""Actions a Click button can perform on the Mac.

Formats accepted in config.toml:
    "obc:shift_up"     OpenBikeControl command sent to MyWhoosh over the network (see obc.BUTTONS);
                       gears and steering fall back to the keyboard if MyWhoosh is not connected
    "obc:steer_left+left"  several commands in the same message: MyWhoosh uses what it needs
    "key:k"            presses and releases a key right away (shift, emote)
    "hold:a"           holds the key down for as long as the button is held (steering)
    "media:playpause"  media keys: playpause, next, previous, volup, voldown
    "screenshot"       saves a screenshot on the Desktop
    "none"             nothing

"key" and "hold" only fire if the chosen app (MyWhoosh) is in front,
so the Click does not type letters into other windows. Media, screenshot and
OpenBikeControl commands always fire.
"""

import datetime
import logging
import subprocess
from pathlib import Path

import Quartz
from AppKit import NSEvent, NSSound, NSWorkspace
from PyObjCTools import AppHelper

import obc
from i18n import _

log = logging.getLogger("click2whoosh")

# If MyWhoosh is not connected over OpenBikeControl, these commands use the keyboard.
OBC_KEYBOARD_FALLBACK = {"shift_up": "key:k", "shift_down": "key:i", "steer_left": "hold:a", "steer_right": "hold:d"}

# macOS key codes (physical layout, independent of the keyboard language).
KEY_CODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    "return": 36, "tab": 48, "space": 49, "escape": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
}

# NX_KEYTYPE_* codes of the media keys.
MEDIA_KEYS = {"volup": 0, "voldown": 1, "playpause": 16, "next": 17, "previous": 18}

# MyWhoosh emotes, on keys 1 to 7.
def emote_names() -> list[str]:
    return [_("peace"), _("wave"), _("fist bump"), _("dab"), _("elbow bump"), _("cheers"), _("thumbs up")]

def action_choices() -> list[tuple[str, str, str]]:
    """Readable names, for the menu and the Settings window: (group, action, label), translated.

    Only what MyWhoosh really acts on: camera, menus, U-turn, ERG and so on are declared by
    MyWhoosh over OpenBikeControl but ignored (BikeControl reports this too).
    """
    my_whoosh, music = "MyWhoosh", _("Music")
    return [
        (my_whoosh, "obc:shift_up", _("Shift up")),
        (my_whoosh, "obc:shift_down", _("Shift down")),
        (my_whoosh, "obc:steer_left", _("Steer left")),
        (my_whoosh, "obc:steer_right", _("Steer right")),
        # Left/right together with steering: to try, for choosing the road at forks.
        (my_whoosh, "obc:steer_left+left", _("Steer left + Left (forks, experimental)")),
        (my_whoosh, "obc:steer_right+right", _("Steer right + Right (forks, experimental)")),
        (music, "media:playpause", _("Play/pause")),
        (music, "media:next", _("Next track")),
        (music, "media:previous", _("Previous track")),
        (music, "media:volup", _("Volume up")),
        (music, "media:voldown", _("Volume down")),
        (_("Mac"), "screenshot", _("Mac screenshot")),
        (_("Keyboard"), "key:u", _("U key (minimal interface)")),
        *((_("Keyboard"), f"key:{n}", _("Emote {n}: {name}").format(n=n, name=name))
          for n, name in enumerate(emote_names(), start=1)),
    ]
# Actions that stay active while the button is held: no double click or long press.
HELD_ACTIONS = {"obc:steer_left", "obc:steer_right", "obc:steer_left+left", "obc:steer_right+right"}
# Default buttons, depending on the Clicks in use (key: whether the left one is used too).
# With the right Click only it does everything; with both, as in Zwift: − and + shift, arrows steer.
# "repeat": buttons that repeat the click while held (e.g. several shifts in a row).
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
    """Whether it makes sense to repeat the action while the button is held."""
    if spec in ("media:volup", "media:voldown"):
        return True
    return spec.startswith(("obc:", "key:")) and spec not in HELD_ACTIONS


def describe(spec: str) -> str:
    for _group, choice, label in action_choices():
        if choice == spec:
            return label
    kind, _sep, arg = spec.partition(":")
    if kind == "key":
        return _("Key {key}").format(key=arg.upper())
    if kind == "hold":
        return _("Hold {key}").format(key=arg.upper())
    return _("Nothing") if spec == "none" else spec


def frontmost_app() -> str:
    """Name of the app in front (e.g. "MyWhoosh Indoor Cycling App")."""
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


# System sounds to confirm the shift: the Click v2 does not vibrate.
SHIFT_SOUNDS = {"up": "Tink", "down": "Pop"}
SHIFT_VOLUME = 0.35
_sounds: dict = {}


def play_shift_sound(direction: str) -> None:
    """Plays on the main thread (NSSound must not be used from other threads)."""
    def play() -> None:
        sound = _sounds.get(direction)
        if sound is None:
            sound = _sounds[direction] = NSSound.soundNamed_(SHIFT_SOUNDS[direction])
            if sound is not None:
                sound.setVolume_(SHIFT_VOLUME)
        if sound is not None:
            sound.stop()  # with repeated shifts the previous sound may still be playing
            sound.play()
    AppHelper.callAfter(play)


def take_screenshot() -> None:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H.%M.%S")
    path = Path.home() / "Desktop" / f"MyWhoosh {stamp}.png"
    subprocess.Popen(["screencapture", "-x", str(path)])
    log.info("screenshot saved to %s", path)


class Action:
    """An action read from config.toml."""

    def __init__(self, spec: str):
        self.spec = spec
        kind, _sep, arg = spec.partition(":")
        self.kind, self.arg = kind, arg
        if kind in ("key", "hold") and arg not in KEY_CODES:
            raise ValueError(_("Unknown key in '{spec}'. Available: {available}").format(spec=spec, available=", ".join(KEY_CODES)))
        if kind == "media" and arg not in MEDIA_KEYS:
            raise ValueError(_("Unknown media key in '{spec}'. Available: {available}").format(
                spec=spec, available=", ".join(MEDIA_KEYS)))
        commands = arg.split("+") if kind == "obc" else []
        for command in commands:
            if command not in obc.BUTTONS:
                raise ValueError(_("Unknown command in '{spec}'. Available: {available}").format(
                    spec=spec, available=", ".join(obc.BUTTONS)))
        self.button_ids = [obc.BUTTONS[c] for c in commands]
        if kind not in ("obc", "key", "hold", "media", "screenshot", "none"):
            raise ValueError(_("Unknown action: '{spec}'").format(spec=spec))
        # Without a network the first command applies: "shift_up+select" becomes the K key.
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
        """"up"/"down" if the action shifts gears (also via the fallback keyboard), otherwise None."""
        if self.kind == "obc" and self.button_ids:
            return {obc.BUTTONS["shift_up"]: "up", obc.BUTTONS["shift_down"]: "down"}.get(self.button_ids[0])
        if self.kind == "key":
            return {"k": "up", "i": "down"}.get(self.arg)
        return None

    @property
    def stays_pressed(self) -> bool:
        """Must be released when the button is released."""
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
