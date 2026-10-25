"""A SQL result whose rows are blank still scrolls to its last row.

The result table is virtualized: the window it paints is worked out by
counting rows at a fixed height (SQL_ROW_H, 20px), and the spacer rows
around it are sized by the same count. A cell with nothing in it has no
line box, so a row of empty strings or NULLs drew at 5px — its padding and
border — and on a mostly-blank answer the scroller ran out of rows before
the window did: the tail of the result could not be scrolled into view at
all. The 20,000-row repro lost the last hundred-odd rows."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# Nine rows in ten blank: the shape of a sparse column, and
# the shape that hid the end of the result.
SPARSE = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 20000) "
          "SELECT CASE WHEN i % 10 = 0 THEN i ELSE '' END AS v, "
          "CASE WHEN i % 10 = 0 THEN 'x' END AS w FROM n")


def _run(page, sql):
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    page.wait_for_function(
        "() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled")
    page.locator("#sqlText").fill(sql)
    page.click("#btnRunSql")
    page.wait_for_selector("#sqlResult table", timeout=60_000)


def _heights(page):
    return page.evaluate("""() => [...document.querySelectorAll('#sqlResult tbody tr[data-i]')]
        .map((tr) => [tr.textContent, tr.getBoundingClientRect().height])""")


def test_a_blank_row_is_as_tall_as_any_other(page):
    _run(page, SPARSE)
    rows = _heights(page)
    blank = [h for text, h in rows if text == ""]
    filled = [h for text, h in rows if text]
    assert blank and filled
    assert max(blank) == pytest.approx(min(filled), abs=0.5), \
        f"a blank row is {min(blank)}px against {min(filled)}px for one with text"


def test_the_last_row_of_a_mostly_blank_result_can_be_scrolled_to(page):
    _run(page, SPARSE)
    # The scroller, measured in one call: a row cell found in one round trip
    # can be repainted away before the next measures it.
    at = page.evaluate("() => { const b = document.querySelector('#sqlResult .sql-result-scroll')"
                       ".getBoundingClientRect(); return [b.x + b.width / 2, b.y + b.height / 2]; }")
    page.mouse.move(*at)
    page.mouse.wheel(0, 10 ** 8)
    # Waited for with its text: the row is painted empty while its page of
    # rows is still being fetched, so a row that is merely there can still
    # read blank.
    page.wait_for_function(
        "() => document.querySelector('#sqlResult tbody tr[data-i=\"19999\"] td')?.textContent === '20000'")
    last = page.locator('#sqlResult tbody tr[data-i="19999"]')
    # On screen, inside the scroller — not merely in the DOM below it.
    row = last.bounding_box()
    pane = page.locator(".sql-result-scroll").bounding_box()
    assert row["y"] >= pane["y"] and row["y"] + row["height"] <= pane["y"] + pane["height"] + 1
