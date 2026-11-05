#!/bin/zsh
# Builds Click2Whoosh.app (signed ad hoc) and the .dmg to share, in packaging/dist.
#   packaging/build.sh
set -euo pipefail
cd "$(dirname "$0")"
PY=../.venv/bin/python

$PY -c 'import PyInstaller' 2>/dev/null || $PY -m pip install -q pyinstaller
$PY -c 'import babel' 2>/dev/null || $PY -m pip install -q babel
# Translations: the .po files are the source, the .mo files (not in git) are what the app reads.
$PY -m babel.messages.frontend compile --statistics -d ../src/locale -D click2whoosh
$PY make_icons.py
$PY -m PyInstaller --noconfirm --clean --distpath dist --workpath build/pyinstaller Click2Whoosh.spec

APP=dist/Click2Whoosh.app
rm -rf dist/Click2Whoosh  # intermediate folder, the app already contains everything
# Translated texts of the permission dialogs (Info.plist has the English ones).
cp -R lproj/*.lproj "$APP/Contents/Resources/"
# Firma col certificato "Click2Whoosh" del Portachiavi, se c'è (README, "From source"): la firma resta
# la stessa a ogni build, quindi Accessibilità e notifiche non vanno ridate. Altrimenti ad-hoc.
IDENTITY="Click2Whoosh"
if security find-identity -p codesigning | grep -q "\"$IDENTITY\""; then SIGN=$IDENTITY; else SIGN=-; fi
echo "Signing: ${SIGN/#-/ad-hoc}"
codesign --force --deep --sign "$SIGN" "$APP"
codesign --verify --strict "$APP"

# Immagine disco da condividere: l'app, un collegamento ad Applicazioni e le istruzioni.
rm -rf build/dmg dist/Click2Whoosh.dmg
mkdir -p build/dmg
ditto "$APP" build/dmg/Click2Whoosh.app
ln -s /Applications build/dmg/Applications
cp README.txt LEGGIMI.txt build/dmg/
hdiutil create -quiet -volname Click2Whoosh -srcfolder build/dmg -format UDZO -ov dist/Click2Whoosh.dmg

# Le copie fuori da Applicazioni non devono contare per macOS: con lo stesso identificativo
# ne sceglie una sola, e se sceglie una di queste Spotlight non mostra quella installata.
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
$LSREGISTER -u "$APP" 2>/dev/null || true
$LSREGISTER -u build/dmg/Click2Whoosh.app 2>/dev/null || true
rm -rf build/dmg
echo "Ready: $APP and dist/Click2Whoosh.dmg"
