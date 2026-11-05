"""Settings window: an HTML page (settings.html) in a native WebKit window (webwindow.py).

Python calls the page's c2wUpdate / c2wStatus / c2wPressed / c2wSaved functions. Every
change is written to config.toml right away and applied to the running program.
"""

import json
import logging
import subprocess
import tomllib
from pathlib import Path
from typing import Callable

from PyObjCTools import AppHelper

from actions import DEFAULT_BUTTONS, DEFAULT_REPEAT_BUTTONS, HELD_ACTIONS, REMOVED_ACTIONS, action_choices, describe, repeatable
from ble import BUTTON_LABELS, SIDE_BUTTONS, SIDE_LEFT, SIDE_RIGHT
from i18n import _
from webwindow import WebWindow

log = logging.getLogger("click2whoosh")
PAGE = "settings.html"


# ---------------------------------------------------------------- config.toml

def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def dump_config(raw: dict) -> str:
    lines = ["# Click2Whoosh configuration, written by the Settings window.",
             "# You can also edit it by hand: the possible actions are described in the README.", ""]
    for section in ("general", "buttons", "long_press", "double_press"):
        lines.append(f"[{section}]")
        for key, value in raw.get(section, {}).items():
            lines.append(f"{key} = {toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def apply_button_change(raw: dict, name: str, field: str, value: str) -> None:
    buttons = raw.setdefault("buttons", {})
    long_press = raw.setdefault("long_press", {})
    double_press = raw.setdefault("double_press", {})
    repeat = raw.setdefault("general", {}).setdefault("repeat_buttons", list(DEFAULT_REPEAT_BUTTONS))

    def set_repeat(on: bool) -> None:
        if on and name not in repeat:
            repeat.append(name)
        elif not on and name in repeat:
            repeat.remove(name)

    if field == "single":
        buttons[name] = value
        if value in HELD_ACTIONS:
            long_press.pop(name, None)
            double_press.pop(name, None)
        if not repeatable(value):
            set_repeat(False)
    elif field == "double":
        if value == "none":
            double_press.pop(name, None)
        else:
            double_press[name] = value
    elif field == "long":
        if value == "repeat":
            long_press.pop(name, None)
            set_repeat(True)
        else:
            set_repeat(False)
            if value == "none":
                long_press.pop(name, None)
            else:
                long_press[name] = value


def switch_default_buttons(raw: dict, left: bool) -> bool:
    """When the left Click is turned on or off: if the buttons are still the defaults of
    the other mode, they switch to the new ones. If you changed them nothing is touched."""
    old, new = DEFAULT_BUTTONS[not left], DEFAULT_BUTTONS[left]
    general = raw.setdefault("general", {})
    if (raw.get("buttons", {}) != old["buttons"]
            or set(general.get("repeat_buttons", DEFAULT_REPEAT_BUTTONS)) != set(old["repeat"])):
        return False
    raw["buttons"] = dict(new["buttons"])
    general["repeat_buttons"] = list(new["repeat"])
    return True


def update_default_buttons(path: Path) -> None:
    """At startup: a configuration never customized but with the other mode's buttons (e.g. created
    when the left Click was not on by default) switches to the right buttons. Others stay as they are."""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    left = raw.get("general", {}).get("use_left_side", True)
    changed = replace_removed_actions(raw)
    if switch_default_buttons(raw, left):
        changed = True
        log.info("buttons updated to the defaults for %s", "both Clicks" if left else "the right Click only")
    if changed:
        path.write_text(dump_config(raw), encoding="utf-8")


def replace_removed_actions(raw: dict) -> bool:
    """Actions that no longer exist (the experimental fork ones) become the plain steering."""
    changed = False
    for section in ("buttons", "long_press", "double_press"):
        table = raw.get(section, {})
        for name, spec in table.items():
            if spec in REMOVED_ACTIONS:
                table[name] = REMOVED_ACTIONS[spec]
                changed = True
    return changed


def button_state(raw: dict, name: str) -> dict:
    single = raw.get("buttons", {}).get(name, "none")
    long_spec = raw.get("long_press", {}).get(name)
    repeat = name in raw.get("general", {}).get("repeat_buttons", DEFAULT_REPEAT_BUTTONS)
    if long_spec is None:
        long_spec = "repeat" if repeat and repeatable(single) else "none"
    return {"single": single, "double": raw.get("double_press", {}).get(name, "none"), "long": long_spec,
            "repeatable": repeatable(single), "held": single in HELD_ACTIONS}


def button_summary(raw: dict, left: bool = False) -> list[dict]:
    """What the buttons do, in words, for the welcome window: the right Click and, if used, the left one."""
    summary = []
    for name in SIDE_BUTTONS[SIDE_RIGHT] + (SIDE_BUTTONS[SIDE_LEFT] if left else ()):
        state = button_state(raw, name)
        extra = {"none": "", "repeat": _("repeats")}
        summary.append({"name": name, "label": BUTTON_LABELS[name], "single": describe(state["single"]),
                        "double": extra.get(state["double"], describe(state["double"])),
                        "long": extra.get(state["long"], describe(state["long"])), "held": state["held"],
                        "side": "left" if name in SIDE_BUTTONS[SIDE_LEFT] else "right"})
    return summary


# ---------------------------------------------------------------- window

class SettingsWindow(WebWindow):
    """Must be created and used on the main thread; pressed() and push_status() can be
    called from any thread. With the window in front (focused) the Click's buttons
    only select."""

    def __init__(self, config_path: Path, get_status: Callable[[], dict],
                 on_saved: Callable[[set], None], current_config: Callable[[], object]):
        super().__init__(_("Click2Whoosh – Settings"), PAGE, 860)
        self.config_path = config_path
        self.get_status = get_status
        self.on_saved = on_saved
        self.current_config = current_config

    def show(self) -> None:
        self.push_state()
        super().show()

    def on_ready(self) -> None:
        self.push_state()

    # ---- from Python to the page

    def push_state(self) -> None:
        """Full state (re-reads config.toml): on opening and after a save."""
        if self.visible:
            self.call("c2wUpdate", self.state())

    def push_status(self) -> None:
        """Only the Click and MyWhoosh status, every second: no disk reads. Main thread."""
        if self.visible:
            self.call("c2wStatus", self.get_status())

    def pressed(self, name: str, is_pressed: bool) -> None:
        """Called from the Bluetooth thread: the window is only touched from the main thread."""
        def show() -> None:
            if self.visible:
                args = ", ".join(json.dumps(a) for a in (name, is_pressed, self.focused))
                self._eval(f"window.c2wPressed && window.c2wPressed({args})")
        AppHelper.callAfter(show)

    def state(self) -> dict:
        raw = self.read_raw()
        config = self.current_config()
        general = {
            "use_left_side": config.use_left_side,
            "long_press_ms": round(config.long_press_s * 1000),
            "double_press_ms": round(config.double_window_s * 1000),
            "battery_warning": config.battery_warning,
            "shift_sound": config.shift_sound,
        }
        actions = [{"group": group, "spec": spec, "label": label, "held": spec in HELD_ACTIONS}
                   for group, spec, label in action_choices()]
        return {
            "buttons": {name: button_state(raw, name) for name in BUTTON_LABELS},
            "labels": BUTTON_LABELS,
            "actions": actions,
            "general": general,
            "status": self.get_status(),
        }

    # ---- from the page to Python

    def read_raw(self) -> dict:
        with self.config_path.open("rb") as f:
            return tomllib.load(f)

    def on_message(self, kind: str, message: dict) -> None:
        if kind == "open_file":
            subprocess.Popen(["open", "-t", str(self.config_path)])
        elif kind in ("button", "general"):
            raw = self.read_raw()
            if kind == "button":
                apply_button_change(raw, str(message["name"]), str(message["field"]), str(message["value"]))
                changed = set()
            else:
                key, value = str(message["key"]), message["value"]
                # From the page come numbers (milliseconds, percentage) or yes/no checkboxes.
                value = bool(value) if key in ("use_left_side", "shift_sound") else int(value)
                raw.setdefault("general", {})[key] = value
                changed = {key}
                if key == "use_left_side" and switch_default_buttons(raw, value):
                    log.info("buttons reset to the defaults for %s", "both Clicks" if value else "the right Click only")
            self.save(raw, changed)

    def save(self, raw: dict, changed: set) -> None:
        old_text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(dump_config(raw), encoding="utf-8")
        try:
            self.on_saved(changed)
        except Exception as e:  # configuration rejected: go back to the previous one
            log.error("invalid setting: %s", e)
            self.config_path.write_text(old_text, encoding="utf-8")
            self._eval(f"alert({json.dumps(_('Invalid setting: {error}').format(error=e))})")
        self.push_state()
        self.call("c2wSaved")
