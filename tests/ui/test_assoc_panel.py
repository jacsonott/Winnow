"""Settings → File associations: the panel renders the catalogue with the
handler/default split intact, and a register round-trips through the real
server (whose XDG dirs the conftest isolates into tmp — nothing here can
touch the developer's real desktop state)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_panel(page):
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator(".settings-section-head", has_text="File associations").click()
    page.wait_for_selector(".assoc-row")


def test_panel_shows_the_catalogue_with_the_default_policy(page):
    _open_panel(page)
    csv = page.locator(".assoc-row", has=page.locator(".assoc-ext", has_text=".csv")).first
    assert csv.locator("button", has_text="Make default").count() == 1
    xlsx = page.locator(".assoc-row", has=page.locator(".assoc-ext", has_text=".xlsx")).first
    # Handler-only types must not offer the default button at all.
    assert xlsx.locator("button").count() == 0
    assert "Open With only" in xlsx.inner_text()
    page.keyboard.press("Escape")


def test_register_round_trips_through_the_panel(page):
    _open_panel(page)
    row = page.locator(".assoc-row", has=page.locator(".assoc-ext", has_text=".tsv")).first
    cb = row.locator("input[type=checkbox]")
    assert not cb.is_checked()
    cb.check()
    # The paint() after the POST re-reads server truth; the box staying
    # checked proves the state came back from disk, not the click.
    page.wait_for_function(
        """() => {
          const rows = [...document.querySelectorAll('.assoc-row')];
          const r = rows.find((x) => x.querySelector('.assoc-ext')?.textContent === '.tsv');
          return r && r.querySelector('input').checked;
        }""")
    cb2 = page.locator(".assoc-row", has=page.locator(".assoc-ext", has_text=".tsv")).first.locator("input")
    cb2.uncheck()
    page.wait_for_function(
        """() => {
          const rows = [...document.querySelectorAll('.assoc-row')];
          const r = rows.find((x) => x.querySelector('.assoc-ext')?.textContent === '.tsv');
          return r && !r.querySelector('input').checked;
        }""")
    page.keyboard.press("Escape")
