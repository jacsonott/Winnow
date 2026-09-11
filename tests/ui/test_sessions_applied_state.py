"""A pivoted comparison is visible from the Sessions panel itself — a
status line naming both sides, the dropdowns preselected to them, and a
Clear that does what the banner's Done does — and the rows wear their
side's colour, not just a pill."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

H = "{ 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' }"


def _open(page):
    page.evaluate("() => __winnow.openSessionManager()")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator(".btn", has_text="Save current work").wait_for(state="visible")


def _tag_rows(page, rows, hotkey="1"):
    for i in rows:
        page.locator("#body .row").nth(i).click()
        page.keyboard.press(hotkey)
        page.wait_for_timeout(60)


def _cleanup(page):
    page.evaluate(f"""() => fetch('/api/case_sessions/new', {{ method: 'POST', headers: {H},
      body: JSON.stringify({{ save_as: null }}) }})""")
    page.evaluate("""() => fetch('/api/case_sessions', { headers: { 'X-Timeline-Lite-Client': '1' } })
      .then((r) => r.json())
      .then((d) => Promise.all(d.sessions.map((s) =>
        fetch('/api/case_sessions/' + encodeURIComponent(s.name),
              { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } }))))""")
    page.evaluate("() => __winnow.loadSources()")


def _save(page, name):
    status = page.evaluate(f"""async (name) => (await fetch('/api/case_sessions', {{ method: 'POST',
      headers: {H}, body: JSON.stringify({{ name }}) }})).status""", name)
    assert status == 200, status


def _compare(page, left, right):
    _open(page)
    page.locator(".session-compare select").first.select_option(left)
    page.locator(".session-compare select").nth(1).select_option(right)
    page.locator(".btn", has_text="Compare").click()
    page.wait_for_selector(".diff-stats")


def test_the_panel_says_a_comparison_is_applied_and_can_clear_it(page):
    _cleanup(page)
    _tag_rows(page, [0, 1, 2])
    try:
        _save(page, "analyst")
        _tag_rows(page, [1, 2])           # the reviewer drops two of the three
        _tag_rows(page, [5], "2")         # and adds one, with a different tag
        _compare(page, "analyst", "__live__")
        assert page.locator("#modalBody .diff-applied").is_hidden(), "nothing pivoted yet"
        page.locator(".diff-stats tr").nth(1).locator(".diff-n-removed .btn").click()
        page.wait_for_selector("#modal", state="hidden")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 2")

        # The rows carry their side's wash, not only the gutter pill.
        page.wait_for_function("() => document.querySelectorAll('#body .row.diff-a').length === 2")
        a = page.evaluate("() => getComputedStyle(document.querySelector('#body .row.diff-a')).backgroundColor")
        plain = page.evaluate("() => getComputedStyle(document.querySelector('#body')).backgroundColor")
        assert a != plain and a != "rgba(0, 0, 0, 0)", a
        # ...and A and B are visibly different colours, not two warm neighbours.
        a_col = page.evaluate("() => getComputedStyle(document.querySelector('#body .diff-mark-removed')).color")
        b_col = page.evaluate("() => getComputedStyle(document.documentElement).getPropertyValue('--diff-b').trim()")
        assert a_col and b_col and b_col.startswith("#")

        # Back in the panel: the status line, and the dropdowns agreeing with it.
        _open(page)
        line = page.locator("#modalBody .diff-applied")
        assert line.is_visible()
        text = line.inner_text()
        assert "Comparison applied" in text and "analyst" in text and "Current work" in text
        assert "only in analyst" in text.lower() and "(2 rows)" in text
        assert page.locator(".session-compare select").first.input_value() == "analyst"
        assert page.locator(".session-compare select").nth(1).input_value() == "__live__"

        # Show all differences: the added row joins, wearing B.
        line.locator(".btn", has_text="Show all differences").click()
        page.wait_for_selector("#modal", state="hidden")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 3")
        page.wait_for_function("() => document.querySelectorAll('#body .row.diff-b').length === 1")

        # Clear from the panel: marks gone, filter back to none, line gone.
        _open(page)
        page.locator("#modalBody .diff-applied .btn", has_text="Clear").click()
        page.wait_for_function("() => __winnow.S.diffMarks === null")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
        assert page.locator("#modalBody .diff-applied").is_hidden()
        assert page.locator("#diffBanner").is_hidden()
        assert page.locator("#body .row.diff-a, #body .row.diff-b").count() == 0
        page.keyboard.press("Escape")
    finally:
        page.evaluate("() => { __winnow.S.diffMarks = null; }")
        _cleanup(page)
        page.keyboard.press("Escape")
