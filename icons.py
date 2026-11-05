"""Icone per la barra dei menu: il logo (menubar.svg), simboli di sistema Apple (SF Symbols) e pallini.

I simboli "template" sono monocromatici e macOS li colora da solo per barra chiara o scura.
Vengono ridisegnati in un'immagine normale: nei menu di rumps le immagini-simbolo così
come arrivano da macOS non comparivano.
"""

from pathlib import Path

from AppKit import (NSBezierPath, NSColor, NSFontWeightRegular, NSImage, NSImageSymbolConfiguration,
                    NSMakeRect)

LOGO_PATH = Path(__file__).with_name("menubar.svg")
_cache: dict = {}


def logo(height: float = 17) -> NSImage | None:
    """Il ciclista del logo in un solo colore: macOS lo fa nero o bianco secondo la barra.

    NSImage legge gli SVG da macOS 14; prima si ripiega sulla bicicletta di sistema.
    """
    key = ("logo", height)
    if key not in _cache:
        image = NSImage.alloc().initWithContentsOfFile_(str(LOGO_PATH))
        if image is None:
            image = symbol("bicycle", 15)
        else:
            width, svg_height = image.size()
            image.setSize_((width * height / svg_height, height))
            image.setTemplate_(True)
        _cache[key] = image
    return _cache[key]


def symbol(name: str, size: float = 14, template: bool = True) -> NSImage | None:
    key = ("symbol", name, size, template)
    if key not in _cache:
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if image is not None:
            config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, NSFontWeightRegular)
            image = image.imageWithSymbolConfiguration_(config)
            width, height = image.size()
            flat = NSImage.alloc().initWithSize_((width, height))
            flat.lockFocus()
            image.drawInRect_(NSMakeRect(0, 0, width, height))
            flat.unlockFocus()
            flat.setTemplate_(template)
            image = flat
        _cache[key] = image
    return _cache[key]


def gray_dot(size: float = 9) -> NSImage:
    """Pallino grigio: Click non collegato (collegato si vede l'icona della batteria)."""
    key = ("dot", size)
    if key not in _cache:
        image = NSImage.alloc().initWithSize_((size, size))
        image.lockFocus()
        NSColor.systemGrayColor().setFill()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(0.5, 0.5, size - 1, size - 1)).fill()
        image.unlockFocus()
        _cache[key] = image
    return _cache[key]


def battery(level: int) -> NSImage | None:
    step = min((0, 25, 50, 75, 100), key=lambda s: abs(s - level))
    return symbol(f"battery.{step}", 13)
