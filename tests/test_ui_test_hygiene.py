"""Traps that make a UI test pass without testing anything.

A UI test that silently does nothing is worse than no test: it reports
green, and the thing it was written to catch keeps happening. These are
checked statically because both traps look completely reasonable on the
page — they read as careful waiting.
"""

from __future__ import annotations

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
