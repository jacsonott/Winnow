"""The SQL result with no row cap, and its actions pinned above it.

Two things were asked for together. The result was capped at 5,000 rows,
and Copy / CSV / Excel export what the pane HOLDS (see
test_sql_result_actions.py for why), so every export of a bigger answer
was silently cut at 5,000 too. And the bar carrying those actions sat
inside the result's scroller, so it scrolled away the moment the analyst
went looking at the rows they wanted to export.

Lifting the cap means a result can be a million rows, which the old
render-every-<tr> table could not survive — so the table is virtualized
like the grid, and a few of these tests pin the ways that goes wrong:
rows past the browser's element-height ceiling unreachable (the grid's
own MAX_SPACER_PX story), and columns that change width as you scroll
because auto layout only ever sees the rows that happen to be painted.

Rows are generated with a recursive CTE, so the shared case needs no
bigger table than the one every other module uses."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui


def _count(n, extra=""):
    return (f"WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < {n}) "
            f"SELECT i{extra} FROM n")


def _run(page, sql):
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    page.wait_for_function(
        "() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled")
    page.locator("#sqlText").fill(sql)
    page.click("#btnRunSql")
    page.wait_for_selector("#sqlResult table", timeout=60_000)


def _scroll_result(page, dy=0, dx=0):
    """Wheel over the result's rows, the way an analyst scrolls it — not by
    setting scrollTop on whichever element the implementation happens to
    scroll — and let the virtualized window repaint. The default is "all
    the way down"."""
    if not dx and not dy:
        dy = 10 ** 8
    box = page.locator("#sqlResult td").first.bounding_box()
    page.mouse.move(box["x"] + 5, box["y"] + 5)
    page.mouse.wheel(dx, dy)
    page.wait_for_timeout(300)


def _hit(page, locator):
    """True when the element is what's actually under its own centre —
    on screen and not scrolled out of its pane or painted over."""
    box = locator.bounding_box()
    return page.evaluate("""([x, y, label]) => {
        const at = document.elementFromPoint(x, y);
        return !!at && !!at.closest('.btn') && at.closest('.btn').textContent.trim() === label;
    }""", [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, locator.inner_text().strip()])


def test_a_result_past_5000_rows_comes_back_whole(page):
    _run(page, _count(12_000))
    status = page.locator(".sql-result-bar .note-status").inner_text()
    assert "12,000 rows" in status
    assert "truncated" not in status


def test_the_actions_stay_in_view_while_the_result_scrolls(page):
    """The symptom: scroll down a result and Copy/CSV/Excel/Save are gone."""
    _run(page, _count(12_000, ", printf('%080d', i) AS wide"))
    for label in ("Copy", "CSV…", "Excel…", "Save as table…"):
        assert _hit(page, page.locator(".sql-result-actions .btn", has_text=label)), label
    _scroll_result(page)
    _scroll_result(page, dx=10_000)
    for label in ("Copy", "CSV…", "Excel…", "Save as table…"):
        assert _hit(page, page.locator(".sql-result-actions .btn", has_text=label)), \
            f"{label} scrolled out of view with the rows"
    # And the header row is pinned too — it always was; the move to an
    # inner scroller must not have cost it.
    head = page.locator("#sqlResult th").first.bounding_box()
    bar = page.locator(".sql-result-bar").bounding_box()
    assert 0 <= head["y"] - (bar["y"] + bar["height"]) < 12, "the header row sits right under the bar"


def test_only_the_visible_window_is_in_the_dom(page):
    _run(page, _count(12_000))
    assert page.locator("#sqlResult tr").count() < 300
    _scroll_result(page)
    last = page.locator("#sqlResult tr").last.locator("td").first.inner_text()
    assert last == "12000", "the last row must be reachable by scrolling"


def test_the_last_of_two_million_rows_is_reachable(page):
    """Two million rows is ~40M px of table at 20px a row, past the height
    Blink will lay an element out at (33,554,365px). Without the grid's
    spacer cap and rescale the tail of the result is simply not there to
    scroll to — a 2.46M-row table once showed only its first 1.4M."""
    _run(page, _count(2_000_000))
    assert "2,000,000 rows" in page.locator(".sql-result-bar .note-status").inner_text()
    _scroll_result(page)
    cells = [c for c in page.locator("#sqlResult td:first-child").all_inner_texts() if c]
    assert cells[-1] == "2000000"
    # The last row is ON SCREEN, not merely in the DOM below it.
    row = page.locator("#sqlResult tr").last.bounding_box()
    pane = page.locator("#sqlResult").bounding_box()
    assert row["y"] + row["height"] <= pane["y"] + pane["height"] + 1


def test_columns_keep_their_width_while_scrolling(page):
    """Auto table layout sizes columns to the rows in the DOM, so a long
    value near the bottom would widen its column only once scrolled to.
    Widths come from every row up front instead."""
    _run(page, _count(12_000, ", CASE WHEN i > 11500 THEN printf('%060d', i) ELSE 'x' END AS v"))
    widths = lambda: page.evaluate(  # noqa: E731
        "() => [...document.querySelectorAll('#sqlResult th')].map((th) => th.getBoundingClientRect().width)")
    before = widths()
    _scroll_result(page)
    after = widths()
    assert after == pytest.approx(before, abs=1)


def test_copy_takes_every_row(page):
    _run(page, _count(7_000))
    page.locator(".sql-result-actions .btn", has_text="Copy").click()
    text = ""
    for _ in range(50):   # polled with evaluate, which awaits — see test_ui_test_hygiene
        text = page.evaluate("() => navigator.clipboard.readText()")
        if text:
            break
        page.wait_for_timeout(100)
    lines = text.split("\n")
    assert lines[0] == "i"
    assert len(lines) == 7_001, "header + every row, not the first 5,000"
    assert lines[-1] == "7000"


def test_excel_takes_every_row(page):
    from openpyxl import load_workbook

    _run(page, _count(7_000))
    page.locator(".sql-result-actions .btn", has_text="Excel").click()
    page.wait_for_selector(".confirm-overlay input")
    page.locator(".confirm-overlay input").fill("all rows")
    with page.expect_download() as dl:
        page.locator(".confirm-card .btn", has_text="Save").first.click()
    ws = load_workbook(io.BytesIO(Path(dl.value.path()).read_bytes())).active
    assert ws.max_row == 7_001
    assert ws.cell(row=7_001, column=1).value == 7000


def test_csv_takes_every_row(page):
    _run(page, _count(7_000))
    page.locator(".sql-result-actions .btn", has_text="CSV").click()
    page.wait_for_selector(".confirm-overlay input")
    page.locator(".confirm-overlay input").fill("all rows")
    with page.expect_download() as dl:
        page.locator(".confirm-card .btn", has_text="Save").first.click()
    lines = Path(dl.value.path()).read_text(encoding="utf-8-sig").strip().splitlines()
    assert len(lines) == 7_001


def test_a_click_sort_orders_the_whole_result(page):
    _run(page, _count(7_000))
    page.locator("#sqlResult th", has_text="i").click()
    page.locator("#sqlResult th", has_text="i").click()   # descending
    page.wait_for_timeout(150)
    first = page.locator("#sqlResult td").first.inner_text()
    assert first == "7000", "the sort must cover rows past the old cap"


def test_tags_join_a_result_with_more_rids_than_fit_an_in_list(page):
    """sqlTagsFor gave up past 5,000 rids — harmless when the result could
    never be bigger, a silently missing Tags column now. Past TAGS_IN_MAX
    it asks for the rid RANGE instead, and must keep only the rows the
    result actually holds. Driven directly with a synthetic result, since
    the shared case's table is 200 rows."""
    tag_id = page.evaluate("() => __winnow.S.tags[0].id")
    page.evaluate("(t) => __winnow.post('/api/row_tags', { pairs: [[1, 150]], tag_id: t, on: true })", tag_id)
    try:
        got = page.evaluate("""async (n) => {
            const m = await import('/static/js/sqlassist.js');
            const rows = (skip) => Array.from({ length: n }, (_, k) => [k + 1]).filter(([r]) => r !== skip);
            const run = async (skip) => {
                const t = await m.sqlTagsFor({ columns: ['rid'], rows: rows(skip) }, 'SELECT rid FROM src_1');
                return t && { has150: '1:150' in t.map, tags: t.map['1:150'] || null, keys: Object.keys(t.map) };
            };
            return { limit: m.TAGS_IN_MAX, held: await run(null), dropped: await run(150) };
        }""", 6_000)
        assert got["held"] is not None, "no Tags column at all past the old cap"
        assert 6_000 > got["limit"], "the synthetic result must take the range path"
        assert got["held"]["tags"] == [tag_id]
        assert got["dropped"] is not None and not got["dropped"]["has150"], \
            "a tagged row inside the range but not in the result must not be carried"
    finally:
        page.evaluate("(t) => __winnow.post('/api/row_tags', { pairs: [[1, 150]], tag_id: t, on: false })", tag_id)
