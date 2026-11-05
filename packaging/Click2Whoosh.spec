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
           (str(ROOT / "common.html"), ".")],
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
        "LSMinimumSystemVersion": "11.0",  # simboli SF dei menu
        "LSUIElement": True,  # solo barra dei menu, niente Dock
        "NSHighResolutionCapable": True,
        "NSBluetoothAlwaysUsageDescription":
            "Click2Whoosh usa il Bluetooth per collegarsi allo Zwift Click e leggere i tasti.",
        "NSLocalNetworkUsageDescription":
            "Click2Whoosh si annuncia sulla rete locale perché MyWhoosh possa collegarsi e ricevere marce e sterzo.",
        "NSBonjourServices": ["_openbikecontrol._tcp"],
    },
)
