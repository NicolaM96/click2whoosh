# PyInstaller: costruisce Click2Whoosh.app (lanciato da build.sh).
import time
from pathlib import Path

ROOT = Path(SPECPATH).parent / "src"
VERSION = "1.0"
# Numero di build crescente: se sul Mac restano altre copie (nel Cestino, su un .dmg montato),
# macOS considera "quella vera" la più recente invece di disattivare quella in Applicazioni.
BUILD = time.strftime("%Y%m%d%H%M")

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "settings.html"), "."), (str(ROOT / "config.toml"), "."),
           (str(ROOT / "menubar.svg"), "."), (str(ROOT / "onboarding.html"), "."),
           (str(ROOT / "common.html"), ".")]
          # Compiled translations (build.sh makes them): locale/<language>/LC_MESSAGES/click2whoosh.mo
          + [(str(mo), str(mo.parent.relative_to(ROOT))) for mo in ROOT.glob("locale/*/LC_MESSAGES/*.mo")],
    # MarkupSafe (used by Jinja2) ships its speedups for one architecture only, and this app is
    # universal2. Without the speedups it falls back to plain Python, which is plenty fast here.
    excludes=["markupsafe._speedups"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Click2Whoosh",
          console=False, target_arch="universal2")
coll = COLLECT(exe, a.binaries, a.datas, name="Click2Whoosh")
app = BUNDLE(
    coll,
    name="Click2Whoosh.app",
    icon=str(Path(SPECPATH) / "Click2Whoosh.icns"),
    bundle_identifier="com.click2whoosh.app",  # lo stesso di login.py
    version=VERSION,
    info_plist={
        "CFBundleName": "Click2Whoosh",
        "CFBundleDisplayName": "Click2Whoosh",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": BUILD,
        "LSApplicationCategoryType": "public.app-category.healthcare-fitness",
        "CFBundleDevelopmentRegion": "en",
        "CFBundleLocalizations": ["en", "it"],  # languages of the app: macOS offers them per app too
        "LSMinimumSystemVersion": "11.0",  # simboli SF dei menu
        "LSUIElement": True,  # solo barra dei menu, niente Dock
        "NSHighResolutionCapable": True,
        # Default (English) texts of the permission dialogs; lproj/*.lproj/InfoPlist.strings translate them.
        "NSBluetoothAlwaysUsageDescription":
            "Click2Whoosh uses Bluetooth to connect to your Zwift Click and read its buttons.",
        "NSLocalNetworkUsageDescription":
            "Click2Whoosh announces itself on the local network so MyWhoosh can connect and receive gears and steering.",
        "NSBonjourServices": ["_openbikecontrol._tcp"],
    },
)
