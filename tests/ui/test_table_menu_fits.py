"""The table menu fits a narrow window: nothing in it scrolls sideways.

The column list was a grid with the implicit `auto` column, which grows
to its widest row's min-content, so one long column name made every row
wider than the list. Below roughly 560px of window, the ⤒ ⤓ buttons on
EVERY row went off the right edge behind a horizontal scrollbar. A long
name now gives way with an ellipsis instead, and keeps its whole text in
the row's tooltip.

It was also the app's default 680px dialog at any window size, which left
a long name little room beside its type and four buttons; it opens as wide
as the other wide dialogs now.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# Narrow enough that the shared table's ExtremelyLongColumnHeaderName
# cannot fit beside its row's buttons.
NARROW = (420, 800)

MEASURE = """() => {
  const body = document.querySelector('#modalBody');
  const list = body.querySelector('.collist');
  const right = list.getBoundingClientRect().left + list.clientWidth;
  const cut = [...list.querySelectorAll('.collist-row')]
    .filter((r) => [...r.querySelectorAll('button')].some((b) => b.getBoundingClientRect().right > right + 0.5))
    .map((r) => r.querySelector('label').textContent);
  return {
    menu: body.scrollWidth - body.clientWidth,
    list: list.scrollWidth - list.clientWidth,
    cut,
  };
}"""


def _open(pg):
    pg.evaluate("() => __winnow.openTableMenu()")
    pg.wait_for_selector("#modalBody .collist-row")


def test_a_narrow_window_shows_every_rows_buttons_without_sideways_scroll(own_page):
    with own_page(viewport=NARROW) as pg:
        _open(pg)
        got = pg.evaluate(MEASURE)
        assert got["menu"] <= 0, f"the table menu scrolls sideways by {got['menu']}px"
        assert got["list"] <= 0, f"the column list scrolls sideways by {got['list']}px"
        assert not got["cut"], f"rows with a button past the list's right edge: {got['cut']}"


def test_a_long_name_is_cut_short_and_kept_whole_in_the_tooltip(own_page):
    with own_page(viewport=NARROW) as pg:
        _open(pg)
        row = pg.locator(".collist-row", has_text="ExtremelyLongColumnHeaderName")
        assert row.locator(".collist-name").evaluate("(e) => e.scrollWidth > e.clientWidth"), \
            "the long name should be what gives way at this width"
        assert row.locator(".collist-type").evaluate("(e) => e.scrollWidth <= e.clientWidth"), \
            "the type should stay whole while the name still has letters to give"
        assert row.locator("label").get_attribute("title").startswith("ExtremelyLongColumnHeaderName ")


def test_a_desktop_window_opens_the_table_menu_wide(own_page):
    with own_page(viewport=(1440, 900)) as pg:
        _open(pg)
        width = pg.locator(".modal-card").first.evaluate("(e) => e.getBoundingClientRect().width")
        assert width >= 900, f"the table menu is {width}px wide in a 1440px window"
