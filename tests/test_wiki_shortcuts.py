"""docs/wiki/Settings-and-shortcuts.md's table of rebindable shortcuts says
what Settings → Keyboard shortcuts says.

The wiki quotes UI labels exactly as the app shows them (CLAUDE.md), and
that table is the one an analyst reads to learn the keys, but nothing held
it to keymap.js: an action relabelled in ACTION_LABELS, a key added to
DEFAULT_KEYMAP, or a new action left the page describing an older build
with every test green. The table menu's label had already drifted when
this test was written. No browser needed — the two objects are literals,
read with esprima the way tests/test_static_syntax.py reads the modules.
"""

from __future__ import annotations

import re
from pathlib import Path

import esprima

ROOT = Path(__file__).resolve().parent.parent
KEYMAP_JS = ROOT / "static" / "js" / "keymap.js"
PAGE = ROOT / "docs" / "wiki" / "Settings-and-shortcuts.md"

# Settings lists g/G apart from the Ctrl and Meta chords (so they can be
# rebound apart); they go to the same row, and the page says it lists each
# pair once, with all its keys. Map the app's label to the page's row.
SAME_ROW = {
    "Jump to first row": "Jump to the first row",
    "Jump to last row": "Jump to the last row",
}


def _literal(name: str) -> dict:
    """An `export const NAME = { ... };` object literal from keymap.js."""
    src = KEYMAP_JS.read_text(encoding="utf-8")
    start = src.index(f"export const {name} = {{")
    end = src.index("\n};", start) + 2
    body = src[start + len(f"export const {name} = "):end]
    obj = esprima.parseScript(f"x = {body};").body[0].expression.right
    out = {}
    for prop in obj.properties:
        key = prop.key.name if prop.key.type == "Identifier" else prop.key.value
        val = prop.value
        out[key] = [e.value for e in val.elements] if val.type == "ArrayExpression" else val.value
    return out


def _key_label(spec: str) -> str:
    """keymap.js keyLabel(): a capital letter after a modifier has its
    Shift spelled out, because 'Ctrl+U' is how Ctrl+Shift+U is stored."""
    m = re.fullmatch(r"((?:Ctrl\+|Alt\+|Meta\+)+)([A-Z])", spec)
    return f"{m.group(1)}Shift+{m.group(2)}" if m else spec


def _app_rows() -> dict[str, set[str]]:
    keymap, labels = _literal("DEFAULT_KEYMAP"), _literal("ACTION_LABELS")
    rows: dict[str, set[str]] = {}
    for action, keys in keymap.items():
        label = labels.get(action, action)
        rows.setdefault(SAME_ROW.get(label, label), set()).update(_key_label(k) for k in keys)
    return rows


def _page_rows() -> dict[str, set[str]]:
    text = PAGE.read_text(encoding="utf-8")
    assert "| Action | Default keys |" in text, "the shortcut table's header changed — update this test"
    table = text.split("| Action | Default keys |", 1)[1].split("\n\n", 1)[0]
    rows: dict[str, set[str]] = {}
    for line in table.strip().splitlines()[1:]:   # [0] is the | --- | --- | rule
        m = re.fullmatch(r"\| (.+) \| (.+) \|", line.strip())
        assert m, f"not a table row: {line!r}"
        action, cell = m.groups()
        assert action not in rows, f"{action!r} is listed twice"
        # `x` is a key; `` ` `` is the backtick key itself; "none" is no key.
        keys = {a or b for a, b in re.findall(r"`` (.+?) ``|`([^`]+)`", cell)}
        assert keys or cell.strip() == "none", f"no keys read from {cell!r}"
        rows[action] = keys
    return rows


def test_the_page_lists_every_action_settings_does():
    missing = sorted(set(_app_rows()) - set(_page_rows()))
    assert missing == [], f"in Settings but not on {PAGE.name}: {missing}"


def test_the_page_lists_no_action_settings_does_not():
    extra = sorted(set(_page_rows()) - set(_app_rows()))
    assert extra == [], f"on {PAGE.name} but not in Settings (renamed or removed?): {extra}"


def test_each_action_carries_the_default_keys_settings_shows():
    app, page = _app_rows(), _page_rows()
    wrong = {a: {"page": sorted(page[a]), "app": sorted(app[a])}
             for a in app.keys() & page.keys() if app[a] != page[a]}
    assert wrong == {}, wrong


def test_the_reader_sees_through_the_awkward_cells():
    """Guards the parsing above from passing by reading nothing: the
    backtick key, a Shift-in-the-capital chord, and a 'none'."""
    page = _page_rows()
    assert page["Show/hide the table sidebar"] == {"`"}
    assert "Ctrl+Shift+U" in page[next(a for a in page if a.startswith("Copy the unique values"))]
    assert page["Filter the column under the cursor"] == set()
