"""Launch at login through a user LaunchAgent.

Only works with the packaged app (Click2Whoosh.app): launched directly by
launchd, Python alone does not have the Bluetooth permission and macOS would close it.
"""

import plistlib
import sys
from pathlib import Path

LABEL = "com.click2whoosh.app"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def app_bundle() -> Path | None:
    """Path of Click2Whoosh.app if we are running inside the packaged app."""
    if not getattr(sys, "frozen", False):
        return None
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


def available() -> bool:
    return app_bundle() is not None


def is_enabled() -> bool:
    return PLIST.exists()


def set_enabled(enabled: bool) -> None:
    if not enabled:
        PLIST.unlink(missing_ok=True)
        return
    bundle = app_bundle()
    if bundle is None:
        raise RuntimeError("only available in the packaged app")
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with PLIST.open("wb") as f:
        plistlib.dump({
            "Label": LABEL,
            "ProgramArguments": ["/usr/bin/open", "-a", str(bundle)],
            "RunAtLoad": True,
            "LimitLoadToSessionType": "Aqua",
        }, f)
