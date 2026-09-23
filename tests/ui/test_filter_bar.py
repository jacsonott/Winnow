"""The filter bar, and the classic filter row behind a setting.

What it replaced was measured, not guessed at: on a seven-table case the
grid carried a filter box under every one of 27 columns and none of them
had anything typed in it, which made the emptiest strip on screen the
second heaviest thing in the viewport after the data itself.

So the default is now a bar that names only the filters that exist, as
chips, and a column's box is revealed under its header when the header's
opener asks for it. The always-on row is a setting rather than a
casualty: typing straight into a column box without looking is the
Timeline Explorer reflex, and plenty of analysts have it.

These tests run against the real default, which the shared browser
context deliberately turns off for every other module (see conftest.py).
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture(autouse=True)
def bar_on(page):
    """The shared context keeps the classic row so the older modules can
    keep typing into `.fcell input`; this file puts the shipped default
    back for itself. The context is thrown away after each test, so there
    is nothing to undo."""
    page.evaluate("""() => { __winnow.S.appearance.filterUi = 'bar';
      __winnow.S.filters = {}; __winnow.S.filterOpen = [];
      __winnow.renderHead(); }""")


def _cols(page) -> int:
    return page.evaluate("() => __winnow.visibleCols().length")


def _rows(page) -> int:
    return page.evaluate("() => __winnow.S.view.row_count")


def _open_box(page, col: str):
    """The one-click path: the opener on the column's own header."""
    page.locator(f'.hcell[data-col="{col}"] .hcell-filter').click()
    page.wait_for_selector(f'.fcell input[data-col="{col}"]')


def _filter(page, col: str, text: str):
    """Open the box, type, commit — which folds the box back into a chip."""
    _open_box(page, col)
    box = page.locator(f'.fcell input[data-col="{col}"]')
    box.fill(text)
    box.press("Enter")
    page.wait_for_selector(f'.filter-chip[data-col="{col}"]')


def test_a_fresh_install_gets_the_bar(page):
    assert page.evaluate("() => __winnow.defaultAppearance().filterUi") == "bar"
    assert page.evaluate("() => __winnow.FILTER_UI_DEFAULT") == "bar"


def test_the_grid_carries_no_empty_filter_boxes(page):
    """The measurement that started this: one box per column, none in use.
    Nothing is filtered here, so the count that matters is zero."""
    assert _cols(page) > 3
    assert page.locator(".fcell input").count() == 0
    assert page.locator("#filterRow").is_hidden()
    bar = page.locator("#filterBar")
    assert bar.is_visible()
    assert "none" in bar.inner_text()


def test_the_bar_names_every_filter_that_is_set(page):
    _filter(page, "EventId", "=4624")
    chip = page.locator('.filter-chip[data-col="EventId"]')
    assert chip.count() == 1
    assert "EventId" in chip.inner_text()
    assert "4624" in chip.inner_text()
    # A second one, on another column, joins it rather than replacing it.
    _filter(page, "Host", "H1")
    assert page.locator(".filter-chip").count() == 2
    assert "H1" in page.locator('.filter-chip[data-col="Host"]').inner_text()


def test_a_chip_says_which_operator_is_in_play(page):
    """`!powershell` and `powershell` are opposite filters. A chip that
    showed the bare text would draw them identically."""
    _filter(page, "Host", "!H1")
    assert "not" in page.locator('.filter-chip[data-col="Host"]').inner_text()


def test_removing_a_chip_clears_that_filter(page):
    everything = _rows(page)
    _filter(page, "EventId", "=4624")
    page.wait_for_function("(n) => __winnow.S.view.row_count < n", arg=everything)
    narrowed = _rows(page)
    assert 0 < narrowed < everything

    page.locator('.filter-chip[data-col="EventId"] .filter-chip-rm').click()
    page.wait_for_function("(n) => __winnow.S.view.row_count === n", arg=everything)
    assert page.evaluate("() => __winnow.S.filters.EventId") in (None, "")
    assert page.locator(".filter-chip").count() == 0


def test_a_chip_reopens_its_box_for_editing(page):
    """Clicking the filter to change it is the gesture people try first,
    and the box it lands in is the one under that column."""
    _filter(page, "EventId", "=4624")
    page.locator('.filter-chip[data-col="EventId"] .filter-chip-label').click()
    box = page.locator('.fcell input[data-col="EventId"]')
    box.wait_for(state="visible")
    assert box.input_value() == "=4624"
    # While it is being edited it is not ALSO a chip — one filter, one place.
    assert page.locator('.filter-chip[data-col="EventId"]').count() == 0


def test_an_opened_box_sits_under_its_own_column(page):
    """Most of the row is empty cells now. They still have to hold their
    columns' widths, or the one box on screen drifts off the header it
    belongs to and the analyst types into the wrong column."""
    _open_box(page, "Host")
    geo = page.evaluate("""() => {
      const h = document.querySelector('.hcell[data-col="Host"]').getBoundingClientRect();
      const f = document.querySelector('.fcell input[data-col="Host"]').closest('.fcell').getBoundingClientRect();
      return { hx: h.x, fx: f.x, hw: h.width, fw: f.width };
    }""")
    assert abs(geo["hx"] - geo["fx"]) < 2, geo
    assert abs(geo["hw"] - geo["fw"]) < 2, geo


def test_escape_puts_the_box_away_again(page):
    _open_box(page, "Host")
    page.locator('.fcell input[data-col="Host"]').press("Escape")
    page.wait_for_selector('.fcell input[data-col="Host"]', state="detached")
    assert page.locator("#filterRow").is_hidden()


def test_the_value_picker_still_opens_from_a_revealed_box(page):
    """The picker hangs off the box's own ▾, and the box is only there on
    request now — so the path to it has to survive the reveal."""
    _open_box(page, "Host")
    page.locator('.fcell-pick[data-col="Host"]').click()
    page.wait_for_selector(".value-picker")
    assert page.locator(".value-picker .vp-row").count() >= 5


def test_the_value_picker_writes_a_chip(page):
    """What the picker applies is an ordinary column filter, so it has to
    show up in the bar like a typed one."""
    _open_box(page, "Host")
    page.locator('.fcell-pick[data-col="Host"]').click()
    page.wait_for_selector(".value-picker")
    page.locator(".value-picker .vp-actions .btn", has_text="None").click()
    page.locator('.value-picker .vp-row:has-text("H1") input').first.check()
    page.locator(".value-picker .vp-actions .btn:not(.ghost)").click()
    page.wait_for_selector('.filter-chip[data-col="Host"], .fcell input[data-col="Host"].active')
    assert page.evaluate("() => __winnow.S.filters.Host") == "=H1"


def test_the_column_picker_opens_a_box_for_the_column_it_names(page):
    page.locator("#filterAdd").click()
    page.wait_for_selector(".filter-col-picker")
    page.locator(".filter-col-picker .vp-search").fill("Host")
    page.locator(".filter-col-picker .menu-item", has_text="Host").click()
    page.wait_for_selector('.fcell input[data-col="Host"]')


def test_the_filter_keybinding_reveals_a_box(page):
    """`focusFilter` ships unbound, but an analyst who binds it means "let
    me type a filter" — and under the bar there is nothing to focus until
    a box is revealed. It aims at the column the cell cursor is in."""
    page.locator(".row").first.locator(".cell").nth(2).click()
    col = page.evaluate("() => __winnow.visibleCols()[__winnow.S.cellRange.c0]")
    page.evaluate("() => __winnow.ACTION_HANDLERS.focusFilter()")
    page.wait_for_function("(c) => !!document.querySelector(`.fcell input[data-col=\"${c}\"]`)", arg=col)
    assert page.evaluate(
        "(c) => document.activeElement === document.querySelector(`.fcell input[data-col=\"${c}\"]`)",
        col) is True


def test_the_setting_restores_the_classic_row(page):
    """The decision this shipped with: the bar is the default and the row
    is one checkbox away, for the analysts who type into it by reflex."""
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    sec = page.locator("#modalBody .settings-section").filter(
        has=page.locator(".settings-section-title", has_text=re.compile(r"^Appearance$")))
    sec.locator(".settings-section-head").click()   # sections start collapsed
    cb = sec.locator("label.check-row", has_text="Always-on filter row").locator("input")
    cb.wait_for(state="visible")
    assert not cb.is_checked()
    cb.check()
    page.wait_for_function("() => __winnow.S.appearance.filterUi === 'row'")
    page.evaluate("() => __winnow.closeModal()")

    # Every column has its box back, the bar is gone, and the choice is
    # remembered for the next time this browser opens Winnow.
    assert page.locator("#filterRow .fcell input").count() == _cols(page)
    assert page.locator("#filterBar").is_hidden()
    assert page.locator(".hcell-filter").count() == 0
    assert page.evaluate(
        "() => JSON.parse(localStorage.getItem('winnow.appearance')).filterUi") == "row"


def test_the_classic_row_keeps_filters_the_bar_set(page):
    """Switching surfaces is a change of clothes, not of state."""
    everything = _rows(page)
    _filter(page, "EventId", "=4624")
    page.wait_for_function("(n) => __winnow.S.view.row_count < n", arg=everything)
    narrowed = _rows(page)
    page.evaluate("() => { __winnow.S.appearance.filterUi = 'row'; __winnow.renderHead(); }")
    assert page.locator('.fcell input[data-col="EventId"]').input_value() == "=4624"
    assert _rows(page) == narrowed
