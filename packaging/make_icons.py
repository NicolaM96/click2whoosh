"""Icona dell'app: icon.svg → PNG di tutte le misure → Click2Whoosh.icns (con iconutil).

Usa il motore SVG di macOS (NSImage legge gli SVG da macOS 14), niente dipendenze in più.
    .venv/bin/python packaging/make_icons.py [--preview]
"""

import shutil
import subprocess
import sys
from pathlib import Path

from AppKit import (NSBitmapImageRep, NSCalibratedRGBColorSpace, NSGraphicsContext, NSImage,
                    NSImageInterpolationHigh, NSMakeRect, NSPNGFileType)

HERE = Path(__file__).parent
SVG = HERE / "icon.svg"
ICONSET = HERE / "build" / "Click2Whoosh.iconset"
ICNS = HERE / "Click2Whoosh.icns"


def render(image: NSImage, size: int, path: Path) -> None:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0)
    NSGraphicsContext.saveGraphicsState()
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    context.setImageInterpolation_(NSImageInterpolationHigh)
    NSGraphicsContext.setCurrentContext_(context)
    image.drawInRect_(NSMakeRect(0, 0, size, size))
    NSGraphicsContext.restoreGraphicsState()
    path.parent.mkdir(parents=True, exist_ok=True)
    rep.representationUsingType_properties_(NSPNGFileType, {}).writeToFile_atomically_(str(path), True)


def main() -> None:
    image = NSImage.alloc().initWithContentsOfFile_(str(SVG))
    if image is None:
        sys.exit(f"non riesco a leggere {SVG}")
    if "--preview" in sys.argv:
        for size in (1024, 128, 32, 16):
            render(image, size, HERE / "build" / f"anteprima_{size}.png")
        return
    shutil.rmtree(ICONSET, ignore_errors=True)
    for base in (16, 32, 128, 256, 512):
        render(image, base, ICONSET / f"icon_{base}x{base}.png")
        render(image, base * 2, ICONSET / f"icon_{base}x{base}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICNS)], check=True)
    print(ICNS)


if __name__ == "__main__":
    main()
