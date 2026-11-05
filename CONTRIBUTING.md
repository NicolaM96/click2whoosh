# Contributing

Thanks for looking. Bug reports, firmware reports and translations are all welcome.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover tests   # no Click or MyWhoosh needed
```

## Translations

The app is in English and Italian and follows the language of the Mac. It uses
[gettext](https://docs.python.org/3/library/gettext.html): the texts in the code and in the
pages are **English**, wrapped in `_("...")`, and every other language is a `.po` file in
`src/locale/<language>/LC_MESSAGES/`.

### Add a language

Say German (`de`):

```bash
.venv/bin/pybabel init -l de -i src/locale/click2whoosh.pot -d src/locale -D click2whoosh
```

1. Translate `src/locale/de/LC_MESSAGES/click2whoosh.po` with [Poedit](https://poedit.net) or any
   text editor: fill every `msgstr`.
2. Add the language to `CFBundleLocalizations` in `packaging/Click2Whoosh.spec`, and the two
   permission texts in `packaging/lproj/de.lproj/InfoPlist.strings` (copy the English file).
3. Try it:
   ```bash
   .venv/bin/pybabel compile -d src/locale -D click2whoosh
   C2W_LANG=de .venv/bin/python src/app.py
   ```
4. Run the tests. They check that nothing is left untranslated and that placeholders match.

Rules for translators:
- Keep placeholders such as `{click}` or `{level}` exactly as they are. You can move them around.
- Keep HTML tags such as `<b>…</b>` in place.
- Product names (Click2Whoosh, MyWhoosh, Zwift Click, OpenBikeControl) stay as they are.
- Log messages are not translated: they stay in English so bug reports are readable.

### Change or add a text in the code

Write it in English inside `_("...")` (Python) or `{{ _("...") }}` (the pages in `src/*.html`,
which are Jinja templates). Never build a sentence by joining pieces: use one text with
placeholders and `.format(...)`, so translators can reorder the words.

Then update the catalogs:

```bash
.venv/bin/pybabel extract -F babel.cfg -o src/locale/click2whoosh.pot --no-location --width 110 .
.venv/bin/pybabel update -i src/locale/click2whoosh.pot -d src/locale -D click2whoosh --width 110
```

and translate the new entries in each `.po`. The tests fail if the `.pot` is out of date.

## Firmware reports

If you have a Click firmware other than 1.1.0, please
[tell me whether it works](../../issues/new?template=firmware.md).
