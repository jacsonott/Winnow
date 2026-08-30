"""Pinned columns: kept visible while the rest scroll sideways.

Only a live document can show this — the assertion that matters is that a
pinned cell is still where it was after a horizontal scroll that moved
everything else."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _widen(page):
    """Every column visible and wide, without saving — enough that the grid
    scrolls horizontally whatever the shared case file was left holding.

    600px, not 300: the fixture has five columns and the viewport is 1500,
    so 300 leaves about 100px of scroll range and two different scroll
    targets both clamp to the same place — which silently made a
    "did it move?" assertion compare a value with itself."""
    page.evaluate("""() => {
      for (const c of __winnow.S.columns) {
        __winnow.S.layout[c.name] = { ...(__winnow.S.layout[c.name] || {}), hidden: false, w: 600 };
      }
      __winnow.renderHead(); __winnow.render();
    }""")
    page.wait_for_timeout(150)


def _unpin_all(page):
    """Clear pins AND persist the clearing. Pinning is a layout property
    saved server-side, and the UI suite shares one case file — an in-memory
    reset would be undone by the next test's page reloading the layout,
    which is what made these pass alone and fail together."""
    page.evaluate("""() => {
      for (const k of Object.keys(__winnow.S.layout)) {
        if (__winnow.S.layout[k].pinned) __winnow.S.layout[k].pinned = false;
      }
      __winnow.renderHead(); __winnow.render();
      __winnow.saveLayout();
      document.getElementById('body').scrollLeft = 0;
    }""")
    # saveLayout is debounced; give it time to reach the server.
    page.wait_for_timeout(600)


def test_alt_click_pins_a_column_and_it_stays_put(page):
    _unpin_all(page)
    try:
        # Guarantee the grid actually overflows sideways. Other modules hide
        # columns and that survives in the saved layout, so the table can be
        # narrower than the viewport — and a scroll that does not scroll
        # would make every assertion below vacuously true. In memory only:
        # persisting these widths would push the mess onto other tests.
        _widen(page)

        # Addressed by NAME throughout: other modules reorder columns and
        # that order is saved in the layout, so "the first cell" is not
        # reliably any particular column.
        head = page.locator('.hcell[data-col="Timestamp"]')
        control = page.locator('.hcell[data-col="CommandLine"]')

        head.click(modifiers=["Alt"])
        page.wait_for_selector('.hcell.pinned[data-col="Timestamp"]')

        def scroll_to(x):
            page.evaluate("(x) => { document.getElementById('body').scrollLeft = x; }", x)
            page.wait_for_timeout(250)

        # A pinned column is not immovable — `sticky` keeps it in flow until
        # it reaches its offset, THEN holds. So the contract is tested across
        # two scrolls: by the first it has arrived, and the second must not
        # move it while it plainly moves everything else.
        # Scroll past the column's OWN position, not a fixed number: where
        # Timestamp sits depends on the column order, which other modules
        # change and the layout remembers. offsetLeft is the in-flow
        # position, unaffected by the sticky shift, so this is where it must
        # have parked by.
        natural = page.evaluate(
            "() => document.querySelector('.hcell[data-col=\"Timestamp\"]').offsetLeft")
        scroll_to(natural + 250)
        first_scroll = page.evaluate("() => document.getElementById('body').scrollLeft")
        assert first_scroll >= natural + 200, (
            f"grid scrolled only to {first_scroll}, short of the {natural + 250} needed "
            "to park the pinned column; nothing below would prove anything")
        parked = head.bounding_box()
        control_first = control.bounding_box()

        scroll_to(first_scroll + 400)
        assert abs(head.bounding_box()["x"] - parked["x"]) < 3, (parked, head.bounding_box())
        # Which means something only because an unpinned column did move.
        second_scroll = page.evaluate("() => document.getElementById('body').scrollLeft")
        assert second_scroll > first_scroll + 300, (first_scroll, second_scroll)
        assert control.bounding_box()["x"] < control_first["x"] - 300

        # The body cell sits under its own header rather than drifting off.
        cell = page.locator('.cell.pinned').first.bounding_box()
        assert abs(cell["x"] - parked["x"]) < 3, (parked, cell)
    finally:
        _unpin_all(page)


def test_alt_click_does_not_also_sort(page):
    """Alt-click is a different gesture, not a sort with a side effect."""
    _unpin_all(page)
    try:
        page.evaluate("() => { __winnow.S.sort = []; __winnow.renderHead(); }")
        page.locator('.hcell[data-col="Host"]').click(modifiers=["Alt"])
        page.wait_for_selector('.hcell.pinned[data-col="Host"]')
        assert page.evaluate("() => __winnow.S.sort") == []
    finally:
        _unpin_all(page)


def test_a_second_pin_sits_beside_the_first(page):
    """Offsets accumulate — the second pinned column stops where the first
    one ends, rather than on top of it."""
    _unpin_all(page)
    try:
        page.locator('.hcell[data-col="Timestamp"]').click(modifiers=["Alt"])
        page.wait_for_selector('.hcell.pinned[data-col="Timestamp"]')
        page.locator('.hcell[data-col="EventId"]').click(modifiers=["Alt"])
        page.wait_for_selector('.hcell.pinned[data-col="EventId"]')

        offsets = page.evaluate("""() => [...document.querySelectorAll('.hcell.pinned')]
          .map((h) => [h.dataset.col, parseFloat(getComputedStyle(h).left)])""")
        assert [o[0] for o in offsets] == ["Timestamp", "EventId"]
        assert offsets[1][1] > offsets[0][1], offsets

        page.evaluate("() => { document.getElementById('body').scrollLeft = 900; }")
        page.wait_for_timeout(300)
        boxes = page.locator(".hcell.pinned").all()
        a, bb = boxes[0].bounding_box(), boxes[1].bounding_box()
        assert bb["x"] >= a["x"] + a["width"] - 2, (a, bb)
    finally:
        _unpin_all(page)


def test_pinning_survives_reopening_the_table(page):
    """It's a layout property, so it is saved with the rest of the layout."""
    _unpin_all(page)
    try:
        page.locator('.hcell[data-col="Host"]').click(modifiers=["Alt"])
        page.wait_for_selector('.hcell.pinned[data-col="Host"]')
        page.wait_for_timeout(600)   # the layout save is debounced

        sid = page.evaluate("() => __winnow.S.sourceId")
        page.evaluate("(id) => __winnow.openSource(id)", sid)
        page.wait_for_function("() => __winnow.S.view")
        page.wait_for_selector('.hcell.pinned[data-col="Host"]')
    finally:
        _unpin_all(page)


def test_the_columns_panel_pins_too(page):
    """The other way in, for anyone who doesn't know the modifier."""
    _unpin_all(page)
    try:
        page.evaluate("() => __winnow.openTableMenu(__winnow.S.sourceId)")
        page.wait_for_selector(".collist-row")
        row = page.locator(".collist-row", has_text="EventId").first
        row.locator(".collist-pin").click()
        page.wait_for_timeout(200)
        assert page.evaluate("() => (__winnow.S.layout['EventId'] || {}).pinned") is True
        page.keyboard.press("Escape")
    finally:
        _unpin_all(page)
