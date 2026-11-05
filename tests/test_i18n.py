"""Language choice and catalog loading."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import i18n


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


class CatalogTest(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_missing_catalog_shows_english(self):
        i18n.set_language("xx")  # no such catalog
        self.assertEqual(i18n._("Quit"), "Quit")


if __name__ == "__main__":
    unittest.main()
