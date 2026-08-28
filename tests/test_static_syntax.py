"""Parse every frontend module, and prove each one imports what it uses.

The frontend has no build step — nothing between an editor and the browser —
so an unbalanced brace, or a function moved to another module without an
import to match, ships as a blank page with the whole backend suite green.

Two checks, both mechanical over the whole of `static/js/`:

- **it parses.** Cheapest possible guard against a typo in 11,000 lines.
- **every identifier resolves.** For each module, the identifiers it uses but
  doesn't define must all be either imported or a browser global (see
  `jsscope.BROWSER_GLOBALS`). This is what makes the single-file-to-modules
  split verifiable rather than hopeful: a missing import is otherwise a
  ReferenceError on whichever code path happens to touch it, which may be an
  error branch nobody runs for months.

`esprima` (the pure-Python port) is ES2017, so `jsscope` rewrites the three
newer syntaxes this codebase uses before parsing. Adding a fourth means
adding a line to its rewrite table.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("esprima", reason="pip install -r requirements-dev.txt")

from jsscope import BROWSER_GLOBALS, duplicate_top_level_bindings, free_identifiers, parse  # noqa: E402

JS_DIR = Path(__file__).resolve().parent.parent / "static" / "js"
MODULES = sorted(JS_DIR.glob("*.js"))


def test_there_are_modules_to_check():
    """Guards against the glob silently going empty — a rename would
    otherwise turn both tests below into vacuous passes."""
    assert len(MODULES) > 10, f"only found {[m.name for m in MODULES]}"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_module_parses(path):
    try:
        parse(path.read_text(encoding="utf-8"), module=True)
    except Exception as e:
        pytest.fail(f"{path.name} does not parse: {e}")


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_module_imports_everything_it_uses(path):
    src = path.read_text(encoding="utf-8")
    tree = parse(src, module=True)
    imported = {
        spec.local.name
        for node in tree.body
        if node.type == "ImportDeclaration"
        for spec in node.specifiers
    }
    unresolved = sorted(free_identifiers(src, module=True) - BROWSER_GLOBALS - imported)
    assert not unresolved, (
        f"{path.name} uses {unresolved} without importing them "
        f"(or they belong in jsscope.BROWSER_GLOBALS)"
    )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_module_declares_each_top_level_name_once(path):
    """A duplicate import is a SyntaxError the browser applies to the whole
    module — a blank app — but it parses, and every name still resolves, so
    the import check above sails past it. Caught in review once; pinned
    here so the next one fails a test instead of a page load."""
    dupes = duplicate_top_level_bindings(path.read_text(encoding="utf-8"))
    assert not dupes, f"{path.name} declares these more than once at top level: {dupes}"


def test_index_html_references_only_files_that_exist():
    """A renamed or deleted asset is a blank page with a 404 in a console
    nobody has open."""
    import re

    root = JS_DIR.parent.parent
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert refs, "index.html stopped referencing any static asset"
    for ref in refs:
        assert (root / ref.lstrip("/")).exists(), f"index.html references missing {ref}"
