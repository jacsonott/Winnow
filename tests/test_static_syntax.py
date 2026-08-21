"""Parse `static/app.js` and reject a syntax error.

The frontend has no build step — nothing between an editor and the browser —
so an unbalanced brace or a broken arrow body in an 11,000-line file ships as
a blank page, and the backend suite passes the whole way. This is the cheapest
possible guard against that: not a linter, not a type checker, just "does it
parse".

`esprima` (the pure-Python port) is ES2017, so the three newer syntaxes this
codebase uses have to be rewritten to older equivalents before parsing. That
rewrite is deliberately dumb — it runs over the whole file, including string
literals — because its output is thrown away and only the parse result is
kept. Adding a syntax esprima doesn't know (optional catch binding aside)
means adding a line here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

esprima = pytest.importorskip("esprima", reason="pip install -r requirements-dev.txt")

APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"

# (pattern, replacement, what it is) — each one is syntax the browser has had
# for years and esprima has not.
ES2017_REWRITES = [
    (re.compile(r"catch\s*\{"), "catch (e) {", "optional catch binding"),
    (re.compile(r"\?\?"), "||", "nullish coalescing"),
    (re.compile(r"\?\."), ".", "optional chaining"),
]


def test_app_js_parses():
    src = APP_JS.read_text(encoding="utf-8")
    for pattern, replacement, _ in ES2017_REWRITES:
        src = pattern.sub(replacement, src)
    try:
        esprima.parseScript(src)
    except Exception as e:  # esprima raises its own Error type
        pytest.fail(f"static/app.js does not parse: {e}")


def test_index_html_references_only_files_that_exist():
    """A renamed or deleted asset is a blank page with a 404 in a console
    nobody has open. Cheap to check while we're here."""
    root = APP_JS.parent.parent
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    for ref in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
        assert (root / ref.lstrip("/")).exists(), f"index.html references missing {ref}"
