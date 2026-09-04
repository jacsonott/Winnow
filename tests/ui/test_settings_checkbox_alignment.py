"""Every checkbox row in Settings lines its label up the same way. The row
that broke this was Windows-only, so it never rendered for anyone
reviewing the four beside it — this test stubs the platform so it does."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui


def _open_appearance(page):
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    head = page.locator(".settings-section-head", has_text="Appearance")
    if head.get_attribute("aria-expanded") != "true":
        head.click()
    page.wait_for_selector("#modalBody .check-row")


def _rows(page):
    """Left edge of each checkbox row's text, and how it lays out."""
    return page.evaluate("""() => [...document.querySelectorAll('#modalBody label')]
        .filter((l) => l.querySelector(':scope > input[type=checkbox]') && l.offsetParent)
        .map((l) => {
          const span = l.querySelector(':scope > span');
          const cb = l.querySelector(':scope > input[type=checkbox]');
          const cs = getComputedStyle(l);
          return { text: (span ? span.textContent : '').slice(0, 30),
                   gap: Math.round(span.getBoundingClientRect().left - cb.getBoundingClientRect().right),
                   display: cs.display, klass: l.className };
        })""")


def test_every_checkbox_row_uses_the_same_layout(page):
    try:
        _open_appearance(page)
        rows = _rows(page)
        assert len(rows) >= 3, rows
        assert {r["display"] for r in rows} == {"flex"}, rows
        assert {r["klass"] for r in rows} == {"check-row"}, rows
        assert len({r["gap"] for r in rows}) == 1, rows
    finally:
        page.keyboard.press("Escape")


def test_the_windows_only_row_lines_up_with_the_rest(page):
    """It renders only where associations exist, so stub the status route
    rather than leave the row untested everywhere but Windows."""
    payload = {"platform": "windows", "background": False, "command": "winnow.exe",
               "types": [], "prompted": [], "asked": []}
    page.route("**/api/assoc/types", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(payload)))
    try:
        _open_appearance(page)
        page.wait_for_selector("#modalBody label:has-text('Start Winnow hidden')")
        rows = _rows(page)
        hidden = [r for r in rows if r["text"].startswith("Start Winnow hidden")]
        assert len(hidden) == 1, rows
        assert len({r["gap"] for r in rows}) == 1, rows      # same offset as its neighbours
        assert hidden[0]["display"] == "flex" and hidden[0]["klass"] == "check-row"
    finally:
        page.unroute("**/api/assoc/types")
        page.keyboard.press("Escape")
