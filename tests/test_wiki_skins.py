"""docs/wiki/Settings-and-shortcuts.md's skin table says what Settings says.

Its second column is headed "Description as shown", and the page quotes UI
labels exactly as the app shows them (CLAUDE.md), but nothing held it to
settings.js's STYLES: Harvest's row carried a "The default." that no card
in Settings shows. Read with esprima, as test_wiki_shortcuts.py reads
keymap.js; no browser needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import esprima

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_JS = ROOT / "static" / "js" / "settings.js"
PAGE = ROOT / "docs" / "wiki" / "Settings-and-shortcuts.md"


def _app_skins() -> list[tuple[str, str]]:
    """(label, desc) per skin, in STYLES order: the order the Skins panel
    shows its cards."""
    src = SETTINGS_JS.read_text(encoding="utf-8")
    start = src.index("export const STYLES = {")
    end = src.index("\n};", start) + 3
    body = src[start + len("export const STYLES = "):end].rstrip(";")
    obj = esprima.parseScript(f"x = {body};").body[0].expression.right
    out = []
    for skin in obj.properties:
        fields = {(p.key.name if p.key.type == "Identifier" else p.key.value): p.value
                  for p in skin.value.properties}
        out.append((fields["label"].value, fields["desc"].value))
    return out


def _page_skins() -> list[tuple[str, str]]:
    text = PAGE.read_text(encoding="utf-8")
    head = "| Skin | Description as shown |"
    assert head in text, "the skin table's header changed — update this test"
    table = text.split(head, 1)[1].split("\n\n", 1)[0]
    rows = []
    for line in table.strip().splitlines()[1:]:   # [0] is the | --- | --- | rule
        m = re.fullmatch(r"\| (.+?) \| (.+) \|", line.strip())
        assert m, f"not a table row: {line!r}"
        rows.append(m.groups())
    return rows


def test_the_page_lists_every_skin_in_the_panels_order():
    assert [name for name, _ in _page_skins()] == [label for label, _ in _app_skins()]


def test_each_description_is_the_one_its_card_shows():
    page = dict(_page_skins())
    wrong = {label: {"page": page.get(label), "app": desc}
             for label, desc in _app_skins() if page.get(label) != desc}
    assert wrong == {}, wrong
