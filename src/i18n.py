"""Translations, with gettext.

The source language is English: the strings in the code are the English text, wrapped in _().
Other languages are catalogs in locale/<language>/LC_MESSAGES/click2whoosh.mo, compiled from the
.po files next to them (see CONTRIBUTING.md). A string with no translation shows in English.

The language comes from macOS (System Settings → Language & Region, also per app). For testing,
the environment variable C2W_LANG forces one:  C2W_LANG=it .venv/bin/python src/app.py
"""

import gettext
import os
from pathlib import Path

DOMAIN = "click2whoosh"
LOCALE_DIR = Path(__file__).with_name("locale")
SOURCE_LANGUAGE = "en"

_translation: gettext.NullTranslations = gettext.NullTranslations()
language: str = SOURCE_LANGUAGE


def available_languages() -> list[str]:
    """The source language and every language with a compiled catalog."""
    compiled = sorted(p.parent.parent.name for p in LOCALE_DIR.glob(f"*/LC_MESSAGES/{DOMAIN}.mo"))
    return [SOURCE_LANGUAGE] + [lang for lang in compiled if lang != SOURCE_LANGUAGE]


def preferred_languages() -> list[str]:
    """The user's languages, most preferred first, as BCP 47 tags ("it-IT")."""
    forced = os.environ.get("C2W_LANG")
    if forced:
        return [forced]
    try:
        from Foundation import NSLocale
        return [str(tag) for tag in NSLocale.preferredLanguages()]
    except ImportError:
        return []


def pick_language(preferred: list[str], available: list[str]) -> str:
    """The first preferred language we have: "pt-BR" matches pt_BR, then pt. Otherwise English."""
    for tag in preferred:
        parts = tag.replace("_", "-").split("-")
        language_only = parts[0].lower()
        candidates = [language_only]
        if len(parts) > 1 and len(parts[1]) == 2:  # a region: pt-BR -> pt_BR
            candidates.insert(0, f"{language_only}_{parts[1].upper()}")
        for candidate in candidates:
            if candidate in available:
                return candidate
    return SOURCE_LANGUAGE


def set_language(code: str, locale_dir: Path | None = None) -> None:
    """Use this language from now on. Strings are translated when _() runs, so call this before
    building anything that holds translated text."""
    global _translation, language
    _translation = gettext.translation(DOMAIN, str(locale_dir or LOCALE_DIR), languages=[code], fallback=True)
    language = code


def gettext_(message: str) -> str:
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)


_ = gettext_
set_language(pick_language(preferred_languages(), available_languages()))
