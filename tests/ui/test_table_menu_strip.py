"""The table menu's foot strip, and the Columns panel's filter box and
end-of-list arrows.

The everyday actions — Tables manager, Reset view — sit along the foot,
under every section. Under a two-hundred-column list they were a scroll
away every time.

Close this tab sat there too, at the right-hand end: the corner where
every other dialog's foot puts Cancel, under a label that starts like the
dialog's own Close. Analysts reaching to put the menu away closed the tab
instead, and lost its filters, search and sort. It lives in Name & merge
now, a pane nobody lands on by accident."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

EXTRA = "closetab.csv"


def _open(page):
    page.evaluate("() => __winnow.openTableMenu()")
    page.wait_for_selector(".collist-row")


def _restore(page, orig):
    page.evaluate("""(o) => { __winnow.S.order = o; __winnow.renderHead(); __winnow.saveLayout(); }""", orig)


def test_the_foot_holds_the_three_everyday_actions_under_every_section(page):
    _open(page)
    for section in ("columns", "valueFilters", "layout", "name"):
        page.locator(f'.side-rail-item[data-section="{section}"]').click()
        page.wait_for_selector(f'.table-menu-pane[data-section="{section}"]')
        strip = page.locator("#modalBody .table-menu-foot .table-menu-strip")
        assert strip.count() == 1, section
        labels = strip.locator("button").all_inner_texts()
        assert labels == ["Tables manager…", "Reset view"], (section, labels)
        # ...and the section itself doesn't repeat them.
        body = page.locator("#modalBody .table-menu-pane").inner_text()
        assert "Tables manager" not in body, section
        if section == "layout":
            assert "Save this layout as the default" in body


@pytest.fixture
def extra(page, tmp_path, imported, api):
    """A second open table, so closing one never takes away the table
    every other module expects to find. It goes again afterwards — the UI
    suite shares one case file."""
    f = tmp_path / EXTRA
    f.write_text("Timestamp,EventId,Host\n2026-03-15 09:00:00,4624,H9\n", encoding="utf-8")
    sid = imported({"path": str(f), "name": EXTRA, "kind": "csv"})[0]
    api(f"/api/source/{sid}/open", "POST", {"open": True})
    page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
    page.locator("#sourceTabs .tab", has_text=EXTRA).first.wait_for()
    try:
        yield sid
    finally:
        page.evaluate("() => { document.getElementById('modal').hidden = true; }")
        api(f"/api/source/{sid}", "DELETE")
        page.evaluate("""() => (async () => {
            __winnow.S.viewCache.clear();
            __winnow.S.sourceId = null;
            await __winnow.loadSources();
          })()""")
        page.wait_for_selector(".row")


def test_close_this_tab_is_out_of_the_dismiss_corner_and_under_name_and_merge(page, extra):
    """The symptom: a button that closes the TAB was the dialog's
    bottom-right button, where a click to put the menu away lands. No
    section's foot may hold it, and nothing but Name & merge offers it."""
    page.evaluate("(id) => __winnow.openTableMenu(id)", extra)
    page.wait_for_selector(".collist-row")
    close = page.locator("#modal button", has_text="Close this tab")
    for section in ("columns", "valueFilters", "layout"):
        page.locator(f'.side-rail-item[data-section="{section}"]').click()
        page.wait_for_selector(f'.table-menu-pane[data-section="{section}"]')
        assert close.count() == 0, section
    page.locator('.side-rail-item[data-section="name"]').click()
    page.wait_for_selector('.table-menu-pane[data-section="name"] .tm-tab-row')
    assert close.count() == 1
    assert page.locator("#modalBody .table-menu-foot button", has_text="Close this tab").count() == 0
    # Clicked there on purpose, it still does what the tab's ✕ does: the
    # dialog goes, the tab goes, the table stays in the case.
    close.click()
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.wait_for_function(
        "(n) => ![...document.querySelectorAll('#sourceTabs .tab')].some((t) => t.textContent.includes(n))",
        arg=EXTRA)
    src = page.evaluate("(id) => __winnow.S.sources.find((s) => s.id === id) || null", extra)
    assert src is not None and src["is_open"] is False


def test_the_filter_box_narrows_the_rows_and_clearing_it_brings_them_back(page):
    _open(page)
    order = page.evaluate("() => [...__winnow.S.order]")
    target = order[1]
    box = page.locator("#modalBody .collist-search")
    box.fill(target.lower())
    visible = page.locator(".collist-row:visible").all_inner_texts()
    assert all(target.lower() in v.lower() for v in visible), visible
    assert any(target in v for v in visible)
    box.fill("")
    assert page.locator(".collist-row:visible").count() == len(order)


def test_the_arrows_send_a_column_to_either_end(page):
    _open(page)
    orig = page.evaluate("() => [...__winnow.S.order]")
    try:
        rows = page.locator(".collist-row")
        # The second row's ⤒: it becomes first in the order, the panel and
        # the grid header alike.
        moved = orig[1]
        rows.nth(1).hover()
        rows.nth(1).locator(".collist-move", has_text="⤒").click()
        assert page.evaluate("() => __winnow.S.order[0]") == moved
        assert page.locator(".collist-row").first.inner_text().startswith(moved) or \
            moved in page.locator(".collist-row").first.inner_text()
        assert page.evaluate("() => document.querySelectorAll('.hcell[data-col]')[0].dataset.col") == moved
        # Then ⤓ on that same row: last in the order.
        page.locator(".collist-row").first.hover()
        page.locator(".collist-row").first.locator(".collist-move", has_text="⤓").click()
        after = page.evaluate("() => [...__winnow.S.order]")
        assert after[-1] == moved
        assert sorted(after) == sorted(orig), "moving must not add or drop columns"
        assert moved in page.locator(".collist-row").last.inner_text()
    finally:
        _restore(page, orig)


def test_moving_a_column_keeps_the_typed_filter(page):
    _open(page)
    orig = page.evaluate("() => [...__winnow.S.order]")
    try:
        target = orig[2]
        page.locator("#modalBody .collist-search").fill(target)
        row = page.locator(".collist-row:visible").first
        row.hover()
        row.locator(".collist-move", has_text="⤒").click()
        assert page.locator("#modalBody .collist-search").input_value() == target
        assert page.evaluate("() => __winnow.S.order[0]") == target
    finally:
        _restore(page, orig)


def test_dragging_a_row_keeps_the_typed_filter(page):
    _open(page)
    orig = page.evaluate("() => [...__winnow.S.order]")
    try:
        # A filter matching at least two rows, then drag the second onto the first.
        page.locator("#modalBody .collist-search").fill("e")
        rows = page.locator(".collist-row:visible")
        assert rows.count() >= 2
        first, second = rows.nth(0).inner_text().split()[0], rows.nth(1).inner_text().split()[0]
        rows.nth(1).drag_to(rows.nth(0), target_position={"x": 10, "y": 3})
        page.wait_for_function("(was) => __winnow.S.order.join('\\n') !== was", arg="\n".join(orig))
        assert page.locator("#modalBody .collist-search").input_value() == "e", "the drop re-rendered the panel"
        after = page.evaluate("() => [...__winnow.S.order]")
        assert after.index(second) < after.index(first)
        shown = [r.split()[0] for r in page.locator(".collist-row:visible").all_inner_texts()]
        assert shown[:2] == [second, first], "the panel's rows follow the new order in place"
    finally:
        _restore(page, orig)
