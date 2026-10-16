"""First/Last's Auto-update switch: on, edits re-run the preview; off,
they mark it stale and Refresh runs it — and the choice is remembered."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


DRAG = """(args) => {
  const [srcSel, dstSel] = args;
  const src = document.querySelector(srcSel);
  const dst = document.querySelector(dstSel);
  const dt = new DataTransfer();
  src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
  dst.dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt, cancelable: true }));
  dst.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt, cancelable: true }));
  src.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
}"""


def test_auto_update_off_waits_for_refresh_and_is_remembered(fl_page):
    pg = fl_page
    auto = pg.locator("input.fl-auto")
    assert auto.is_checked(), "on by default"
    # A grouping: the preview runs on its own.
    pg.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    pg.wait_for_selector("table tbody tr", timeout=10_000)
    first_rows = pg.locator("table tbody tr").count()
    assert first_rows > 0

    # Nothing to run (no grouping) is not "pending": the stale marker clears.
    auto.uncheck()
    pg.locator("[data-zone='groupBy'] [data-field='Host'] button, [data-zone='groupBy'] .chip-rm, [data-zone='groupBy'] button").first.click()
    pg.locator("button.fl-refresh").click()   # with nothing to run, it clears at once
    assert not pg.evaluate("() => [...document.querySelectorAll('.note-status')].some((n) => n.textContent === 'Changed — press Refresh')")
    pg.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    auto.check()
    pg.wait_for_selector("table tbody tr", timeout=10_000)

    # Off: a description edit marks the preview stale, nothing re-runs.
    auto.uncheck()
    assert pg.evaluate("() => localStorage.getItem('winnow.firstlast.auto')") == "0"
    tmpl = pg.locator("input[style*='var(--mono)']").first
    tmpl.fill("{which} — {count} events")
    pg.wait_for_function("() => [...document.querySelectorAll('.note-status')].some((n) => n.textContent === 'Changed — press Refresh')")
    pg.wait_for_timeout(600)   # past the debounce: still the old description
    assert "events" not in pg.locator("table tbody tr").first.inner_text()

    # Refresh runs it.
    pg.locator("button.fl-refresh").click()
    pg.wait_for_function("() => /events/.test(document.querySelector('table tbody tr').textContent)", timeout=10_000)
    assert not pg.evaluate("() => [...document.querySelectorAll('.note-status')].some((n) => n.textContent === 'Changed — press Refresh')")

    # Back on: pending edits catch up without a click.
    tmpl.fill("{which} of {count}")
    pg.wait_for_function("() => [...document.querySelectorAll('.note-status')].some((n) => n.textContent === 'Changed — press Refresh')")
    auto.check()
    pg.wait_for_function("() => !/events/.test(document.querySelector('table tbody tr').textContent)", timeout=10_000)
    assert pg.evaluate("() => localStorage.getItem('winnow.firstlast.auto')") == "1"


def test_a_total_up_column_offers_a_sum_chip(fl_page):
    pg = fl_page
    if not pg.locator("input.fl-auto").is_checked():
        pg.locator("input.fl-auto").check()
    pg.evaluate(DRAG, ["[data-field='EventId']", "[data-zone='sums']"])
    pg.wait_for_selector("button:has-text('{sum:EventId}')", timeout=5_000)
    assert pg.locator("button", has_text="{min:EventId}").count() == 1
    assert pg.locator("button", has_text="{max:EventId}").count() == 1
    pg.locator("button", has_text="{sum:EventId}").click()
    tmpl_value = pg.evaluate("() => document.querySelector(\"input[style*='var(--mono)']\").value")
    assert "{sum:EventId}" in tmpl_value
    # The chip inserted the placeholder; now a template that renders the
    # total on its own so the assertion is on the NUMBER — every group's
    # EventId total is a 4+ digit figure here, and a bare "First of N"
    # must not satisfy this.
    assert "{sum:EventId}" in pg.evaluate("() => document.querySelector(\"input[style*='var(--mono)']\").value")
    pg.locator("input[style*='var(--mono)']").first.fill("{which} of {count} — {sum:EventId}")
    pg.wait_for_function("() => /^(First|Last|Only) of \\d+ — \\d{4,}$/.test(document.querySelector('table tbody tr td:last-child').textContent.trim())", timeout=10_000)
