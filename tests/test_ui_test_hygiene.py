"""Traps that make a UI test pass without testing anything.

A UI test that silently does nothing is worse than no test: it reports
green, and the thing it was written to catch keeps happening. These are
checked statically because both traps look completely reasonable on the
page — they read as careful waiting.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent / "ui"
FILES = sorted(UI.glob("test_*.py"))


def test_there_are_ui_tests_to_check():
    assert len(FILES) > 20, "the scan found almost no UI tests — has the layout changed?"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_wait_on_a_promise_predicate(path):
    """`page.wait_for_function` does NOT await its predicate. Hand it
    `() => fetch(...).then(...)` and it receives a Promise, which is
    truthy, so the wait returns immediately and the test races whatever it
    meant to wait for.

    This has bitten four times, most recently as a merge-builder test that
    failed only on loaded CI runners — where the import it wasn't waiting
    for was slow enough to lose the race. Poll from Python with
    `page.evaluate`, which does await.
    """
    src = path.read_text(encoding="utf-8")
    hits = []
    for m in re.finditer(r"wait_for_function\(\s*(\"\"\"|['\"])(.*?)\1", src, re.S):
        pred = m.group(2)
        if ".then(" in pred or re.search(r"\b(async|await)\b", pred):
            hits.append(src[:m.start()].count("\n") + 1)
    assert not hits, (
        f"{path.name} waits on a promise-returning predicate at line(s) {hits} — "
        "that wait passes instantly. Poll with page.evaluate instead."
    )


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_bare_expect_without_assertion(path):
    """`page.locator(...)` on its own line does nothing — no wait, no
    check. It reads like an assertion and is one only in Playwright's
    `expect()` form, which this suite does not use."""
    bad = [i + 1 for i, line in enumerate(path.read_text(encoding="utf-8").splitlines())
           if re.match(r"^\s*page\.locator\([^)]*\)\s*$", line)]
    assert not bad, f"{path.name}: bare locator with no assertion at line(s) {bad}"


def _fixed_waits_then_asserts(src: str) -> list[int]:
    lines = src.splitlines()
    hits = []
    for node in ast.walk(ast.parse(src)):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(node, field, None)
            if not isinstance(body, list):
                continue
            for wait, nxt in zip(body, body[1:]):
                call = wait.value if isinstance(wait, ast.Expr) else None
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "wait_for_timeout"):
                    continue
                if not isinstance(nxt, ast.Assert):
                    continue
                said = "#" in lines[wait.lineno - 1] or lines[wait.lineno - 2].strip().startswith("#")
                if not said:
                    hits.append(wait.lineno)
    return hits


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_unexplained_fixed_wait_before_an_assert(path):
    """`page.wait_for_timeout(n)` then `assert` is a guess at how long the
    app takes. Too short and the test fails on a loaded CI runner; long
    enough and it is slow everywhere else. Either wait for the thing being
    asserted (`until`, `painted`, `view_rebuilt`, `copied` and the rest in
    tests/ui/conftest.py), or, when the wait is the point (a check that
    something did NOT happen has nothing to wait for), say so in a comment
    on the wait or the line above it."""
    hits = _fixed_waits_then_asserts(path.read_text(encoding="utf-8"))
    assert not hits, (
        f"{path.name}: a fixed wait goes straight into an assert at line(s) {hits}. "
        "Wait for what the assert checks, or comment why a fixed wait is right."
    )


def test_the_fixed_wait_check_sees_what_it_is_for():
    flagged = _fixed_waits_then_asserts(
        "def t(page):\n"
        "    page.wait_for_timeout(300)\n"
        "    assert page.evaluate('1')\n"
    )
    assert flagged == [2]
    explained = _fixed_waits_then_asserts(
        "def t(page):\n"
        "    # a second click would have opened it by now\n"
        "    page.wait_for_timeout(300)\n"
        "    assert page.evaluate('1')\n"
        "    page.wait_for_timeout(300)   # nothing to wait for\n"
        "    assert page.evaluate('1')\n"
    )
    assert explained == []


def test_module_basenames_are_unique_across_tests_and_ui():
    """pytest imports test modules by basename (no __init__.py here), so
    tests/test_x.py and tests/ui/test_x.py collide: the second one to be
    collected errors with "import file mismatch" and interrupts the whole
    run — a backend-only run (`-m "not ui"`) included, since collection
    happens before marker selection. Happened twice; now it fails here,
    with names."""
    tests = Path(__file__).resolve().parent
    seen = {}
    dupes = []
    for path in sorted(tests.rglob("test_*.py")):
        if seen.setdefault(path.name, path) != path:
            dupes.append(f"{path.name}: {seen[path.name].relative_to(tests)} and {path.relative_to(tests)}")
    assert not dupes, "test module basenames must be unique across tests/ and tests/ui/:\n" + "\n".join(dupes)
