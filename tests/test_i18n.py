"""Translations: language choice, catalog quality, and no untranslated text left in the code.

The catalogs are checked from their .po files (compiled into a temporary folder here), so these
tests don't depend on packaging/build.sh having run. Needs Babel (requirements-dev.txt).
"""

import ast
import io
import re
import sys
import tempfile
import tokenize
import unittest
from pathlib import Path

from babel.messages.extract import extract_from_dir
from babel.messages.frontend import parse_mapping_cfg
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import i18n

LOCALE_DIR = ROOT / "src" / "locale"
POT = LOCALE_DIR / "click2whoosh.pot"
LANGUAGES = sorted(p.parent.parent.name for p in LOCALE_DIR.glob("*/LC_MESSAGES/click2whoosh.po"))
PLACEHOLDER = re.compile(r"\{(\w+)\}")
TAG = re.compile(r"</?\w+[^>]*>")
# Letters that give away Italian text in code and pages that should be English.
ITALIAN_LETTERS = re.compile("[àèéìòù]")
COMMENTS = re.compile(r"<!--.*?-->|/\*.*?\*/|(?<![:\w])//[^\n]*", re.DOTALL)  # HTML, CSS and JS comments


def load(path: Path):
    with path.open("rb") as f:
        return read_po(f)


def compile_catalog(language: str, target: Path) -> Path:
    """Compile <language>.po into target/<language>/LC_MESSAGES/click2whoosh.mo."""
    out = target / language / "LC_MESSAGES"
    out.mkdir(parents=True)
    with (out / "click2whoosh.mo").open("wb") as f:
        write_mo(f, load(LOCALE_DIR / language / "LC_MESSAGES" / "click2whoosh.po"))
    return target


class PickLanguageTest(unittest.TestCase):
    def test_first_available_preferred_language_wins(self):
        self.assertEqual(i18n.pick_language(["it-IT", "en-US"], ["en", "it"]), "it")
        self.assertEqual(i18n.pick_language(["de-DE", "it-IT"], ["en", "it"]), "it")

    def test_falls_back_to_english(self):
        self.assertEqual(i18n.pick_language(["de-DE", "fr"], ["en", "it"]), "en")
        self.assertEqual(i18n.pick_language([], ["en", "it"]), "en")

    def test_region_then_language(self):
        self.assertEqual(i18n.pick_language(["pt-BR"], ["en", "pt_BR", "pt"]), "pt_BR")
        self.assertEqual(i18n.pick_language(["pt-PT"], ["en", "pt"]), "pt")
        self.assertEqual(i18n.pick_language(["zh-Hans-CN"], ["en", "zh"]), "zh")


class CatalogQualityTest(unittest.TestCase):
    def test_there_is_a_catalog_besides_english(self):
        self.assertIn("it", LANGUAGES)

    def test_every_message_is_translated(self):
        for language in LANGUAGES:
            catalog = load(LOCALE_DIR / language / "LC_MESSAGES" / "click2whoosh.po")
            for message in catalog:
                if not message.id:
                    continue
                with self.subTest(language=language, message=message.id):
                    self.assertTrue(message.string, "no translation")
                    self.assertNotIn("fuzzy", message.flags, "marked fuzzy: review it and remove the flag")

    def test_placeholders_and_tags_match_the_english(self):
        # A missing or misspelled {name} would crash or blank the text at run time.
        for language in LANGUAGES:
            catalog = load(LOCALE_DIR / language / "LC_MESSAGES" / "click2whoosh.po")
            for message in catalog:
                if not message.id or not message.string:
                    continue
                with self.subTest(language=language, message=message.id):
                    self.assertEqual(sorted(PLACEHOLDER.findall(message.id)),
                                     sorted(PLACEHOLDER.findall(message.string)))
                    self.assertEqual(sorted(TAG.findall(message.id)), sorted(TAG.findall(message.string)))

    def test_template_is_up_to_date_with_the_code(self):
        # Fails when a string was added or changed in the code without running pybabel extract.
        with (ROOT / "babel.cfg").open() as f:
            method_map, options_map = parse_mapping_cfg(f)
        in_code = {message for _file, _line, message, _comments, _context
                   in extract_from_dir(str(ROOT), method_map, options_map, keywords={"_": None})
                   if isinstance(message, str)}
        in_template = {m.id for m in load(POT) if m.id}
        self.assertEqual(in_code - in_template, set(), "in the code but not in the .pot: run pybabel extract")
        self.assertEqual(in_template - in_code, set(), "in the .pot but not in the code: run pybabel extract")


class NoLeftoverItalianTest(unittest.TestCase):
    """The source language is English: Italian text outside the catalogs is a string that was missed."""

    def test_no_italian_in_python_strings(self):
        for path in sorted((ROOT / "src").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                          if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                          and node.body and isinstance(node.body[0], ast.Expr)
                          and isinstance(node.body[0].value, ast.Constant)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    with self.subTest(file=path.name, line=node.lineno):
                        self.assertIsNone(ITALIAN_LETTERS.search(node.value), node.value)

    def test_no_italian_in_python_comments(self):
        for path in sorted((ROOT / "src").glob("*.py")) + sorted((ROOT / "tests").glob("*.py")):
            with path.open(encoding="utf-8") as f:
                for token in tokenize.generate_tokens(f.readline):
                    if token.type == tokenize.COMMENT:
                        with self.subTest(file=path.name, line=token.start[0]):
                            self.assertIsNone(ITALIAN_LETTERS.search(token.string), token.string)

    def test_no_italian_in_page_comments(self):
        for path in sorted((ROOT / "src").glob("*.html")):
            with self.subTest(file=path.name):
                comments = " ".join(match.group(0) for match in COMMENTS.finditer(path.read_text(encoding="utf-8")))
                self.assertIsNone(ITALIAN_LETTERS.search(comments))

    def test_english_pages_have_no_italian(self):
        import webwindow
        i18n.set_language("en")
        for page in ("settings.html", "onboarding.html"):
            with self.subTest(page=page):
                html = webwindow.page_html(page)
                self.assertIsNone(ITALIAN_LETTERS.search(COMMENTS.sub("", html)))  # the comments are not shown
                self.assertNotIn("{{", html)  # every template expression was rendered


class ItalianAtRunTimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        i18n.set_language("it", compile_catalog("it", Path(self.tmp.name)))

    def tearDown(self):
        i18n.set_language("en")
        self.tmp.cleanup()

    def test_texts_are_translated(self):
        import actions
        self.assertEqual(i18n._("Quit"), "Esci")
        self.assertEqual(actions.describe("obc:shift_up"), "Marcia su")
        self.assertEqual(actions.describe("key:u"), "Tasto U (interfaccia minimale)")
        self.assertEqual(actions.describe("hold:a"), "Tieni premuto A")

    def test_untranslated_text_shows_in_english(self):
        self.assertEqual(i18n._("A sentence nobody translated"), "A sentence nobody translated")

    def test_pages_come_out_in_italian(self):
        import webwindow
        html = webwindow.page_html("settings.html")
        self.assertIn('lang="it"', html)
        self.assertIn("Impostazioni", html)
        self.assertNotIn("{{", html)

    def test_status_and_notification_texts(self):
        import app
        self.assertEqual(app.status_text("connected"), "collegato")
        self.assertEqual(app.click_name("left"), "Click sinistro")
        self.assertEqual(i18n._("{click} battery at {level}%").format(click=app.click_name("left"), level=15),
                         "Batteria del Click sinistro al 15%")


class SourceLanguageTest(unittest.TestCase):
    def test_missing_catalog_shows_english(self):
        i18n.set_language("xx")  # no such catalog
        try:
            self.assertEqual(i18n._("Quit"), "Quit")
        finally:
            i18n.set_language("en")


if __name__ == "__main__":
    unittest.main()
