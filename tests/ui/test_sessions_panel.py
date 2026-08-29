"""The Sessions panel: save, fresh pass, and the QC diff.

The diff is the reason this panel exists, so the test that matters is the
one that tags rows, saves, changes the conclusions, and reads what the
comparison actually renders — not just that a table appeared."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


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
    """Sessions AND tags. The UI suite shares one case file, so a test that
    leaves rows tagged decides the next test's starting state — these
    passed individually and failed together until this cleared both."""
    page.evaluate("""() => fetch('/api/case_sessions/new', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ save_as: null }) })""")
    page.evaluate("""() => fetch('/api/case_sessions', { headers: { 'X-Timeline-Lite-Client': '1' } })
      .then((r) => r.json())
      .then((d) => Promise.all(d.sessions.map((s) =>
        fetch('/api/case_sessions/' + encodeURIComponent(s.name),
              { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } }))))""")
    page.evaluate("() => __winnow.loadSources()")


def test_saving_a_session_lists_it_with_its_tag_count(page):
    _cleanup(page)
    _tag_rows(page, [0, 1])
    try:
        _open(page)
        page.locator(".btn", has_text="Save current work").click()
        page.wait_for_selector(".confirm-overlay input")
        page.locator(".confirm-overlay input").fill("first pass")
        page.locator(".confirm-card .btn", has_text="OK").first.click()
        page.wait_for_selector(".session-row .session-name")

        row = page.locator(".session-row", has_text="first pass")
        assert row.count() == 1
        assert "2 tagged" in row.inner_text()
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_the_diff_shows_what_a_reviewer_added_and_removed(page):
    """The QC question, end to end."""
    _cleanup(page)
    _tag_rows(page, [0, 1])
    try:
        page.evaluate("""() => fetch('/api/case_sessions', { method: 'POST',
          headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: 'analyst' }) })""")
        page.wait_for_timeout(200)
        _tag_rows(page, [1])          # reviewer removes row 1 (toggles it off)
        _tag_rows(page, [2])          # and adds row 2

        _open(page)
        page.locator(".session-compare select").first.select_option("analyst")
        page.locator(".session-compare select").nth(1).select_option("__live__")
        page.locator(".btn", has_text="Compare").click()
        page.wait_for_selector(".diff-table")

        # Lowercased: the section headings are CSS-uppercased in the panel.
        text = page.locator(".session-diff").inner_text().lower()
        assert "added on the right" in text, text
        assert "removed on the right" in text, text
        # The rows carry what changed, not just that something did.
        assert "→ ta" in text.replace("\t", " ") or "→\tta" in text
        # The rows themselves, not just the headings.
        assert page.locator(".diff-added").count() >= 1
        assert page.locator(".diff-removed").count() >= 1
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_identical_sessions_say_so_rather_than_showing_an_empty_table(page):
    _cleanup(page)
    _tag_rows(page, [0])
    try:
        page.evaluate("""() => fetch('/api/case_sessions', { method: 'POST',
          headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: 'same' }) })""")
        page.wait_for_timeout(200)
        _open(page)
        page.locator(".session-compare select").first.select_option("same")
        page.locator(".session-compare select").nth(1).select_option("__live__")
        page.locator(".btn", has_text="Compare").click()
        page.wait_for_selector("text=No differences")
    finally:
        _cleanup(page)
        page.keyboard.press("Escape")


def test_a_fresh_pass_saves_then_clears(page):
    _cleanup(page)
    _tag_rows(page, [0, 1])
    try:
        _open(page)
        page.locator(".btn", has_text="Start a fresh pass").click()
        page.wait_for_selector(".confirm-overlay input")
        page.locator(".confirm-overlay input").fill("pass one")
        page.locator(".confirm-card .btn", has_text="OK").first.click()
        page.wait_for_selector("#modal", state="hidden", timeout=15000)

        # Cleared in the case...
        counts = page.evaluate("""() => fetch('/api/tags?source_id=' + __winnow.S.sourceId,
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
        assert sum(counts.get("counts", {}).values()) == 0
        # ...and recoverable.
        saved = page.evaluate("""() => fetch('/api/case_sessions',
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
        assert [s["name"] for s in saved["sessions"]] == ["pass one"]
        assert saved["sessions"][0]["tagged_rows"] == 2
    finally:
        _cleanup(page)
        page.evaluate("() => __winnow.loadSources()")
