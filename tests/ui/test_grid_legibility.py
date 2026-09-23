"""Reading the grid: what a cut-off cell says, how a row says it opens,
what the stats line is about, and the detail pane's field list.

Four measured complaints, all of them about the grid failing to say
something it already knew:

- a cell clipped to `Informati…` carried an empty `title`, so the only way
  to read the value was to widen the column or open the row — while tabs,
  sidebar rows and column headers have always had one;
- nothing on screen mentioned that a row opens on double-click (a single
  click selects a *cell*), which was written down in one source comment
  and nowhere else;
- the one sentence that says what is on screen ended `· cached`, an
  implementation detail about the server's view table;
- the detail pane's field names were uppercased by CSS, and the only copy
  it offered was the whole row.

Each test asserts the symptom in the terms it was wrong in: the title
attribute, the rendered case (`text-transform`), the sentence itself.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui


def _col_index(page, name):
    return page.evaluate("(c) => __winnow.visibleCols().indexOf(c)", name)


def _cell(page, row, name):
    return page.locator("#body .row").nth(row).locator(".cell").nth(_col_index(page, name))


def _raw(page, row, name):
    return page.evaluate(
        "(c) => __winnow.rowAt(%d).cells[__winnow.S.columns.findIndex((x) => x.name === c)]" % row, name)


def _set_width(page, name, width):
    page.evaluate(
        """([c, w]) => {
             __winnow.S.layout[c] = Object.assign({}, __winnow.S.layout[c] || {}, { w });
             __winnow.render();
           }""", [name, width])


# ------------------------------------------------------------ clipped cells

def test_a_clipped_cell_carries_its_full_value(page):
    """CommandLine is 300-odd characters in a default-width column."""
    cell = _cell(page, 0, "CommandLine")
    assert cell.evaluate("(c) => c.scrollWidth > c.clientWidth"), "expected the column to cut this value"
    assert cell.get_attribute("title") == _raw(page, 0, "CommandLine")


def test_a_cell_that_fits_carries_no_title_at_all(page):
    """Not set unconditionally: a title on every cell of every row is DOM
    string for the ones that need none, and a tooltip repeating the text
    already under the pointer is noise."""
    assert _cell(page, 0, "Host").get_attribute("title") is None
    assert _cell(page, 0, "EventId").get_attribute("title") is None


def test_widening_the_column_takes_the_title_away(page):
    """The measurement is the whole point — the title follows whether the
    value is *actually* clipped, not whether the column is a long one."""
    assert _cell(page, 0, "CommandLine").get_attribute("title") is not None
    _set_width(page, "CommandLine", 4000)
    assert _cell(page, 0, "CommandLine").get_attribute("title") is None
    _set_width(page, "CommandLine", 200)
    assert _cell(page, 0, "CommandLine").get_attribute("title") is not None


def test_rows_scrolled_into_view_are_titled_too(page):
    """Every paint pass, not the first one: the grid replaces its rows on
    every scroll frame."""
    page.evaluate("() => { document.getElementById('body').scrollTop = 24 * 60; }")
    page.wait_for_function("() => Number(document.querySelector('#body .row').dataset.pos) > 40")
    row = page.evaluate("() => Number(document.querySelector('#body .row').dataset.pos)")
    cell = _cell(page, 0, "CommandLine")
    assert cell.get_attribute("title") == _raw(page, row, "CommandLine")


# -------------------------------------------------------------- opening a row

def test_the_hovered_row_offers_a_way_to_open_itself(page):
    row = page.locator("#body .row").nth(2)
    opener = row.locator(".row-open")
    assert opener.count() == 1
    assert not opener.is_visible(), "the affordance is for the hovered row only"
    row.hover()
    assert opener.is_visible()


def test_clicking_it_opens_that_row(page):
    row = page.locator("#body .row").nth(2)
    rid = page.evaluate("() => __winnow.rowAt(2).rid")
    row.hover()
    row.locator(".row-open").click()
    page.wait_for_selector("#detail:not([hidden])")
    assert page.locator("#detailTitle").inner_text() == f"Line {rid}"
    # The pane, its note box and "Copy row" all read the cursor row, so
    # opening a row has to move the cursor onto it.
    assert page.evaluate("() => __winnow.S.cursor") == 2


def test_the_gestures_it_advertises_still_work_unchanged(page):
    """This is about saying what already happens — a single click still
    selects a cell and opens nothing, a double-click still opens the row."""
    page.locator("#body .row").nth(3).locator(".cell").first.click()
    page.wait_for_function("() => __winnow.S.cursor === 3")
    assert page.locator("#detail").is_hidden()
    page.locator("#body .row").nth(3).locator(".cell").first.dblclick()
    page.wait_for_selector("#detail:not([hidden])")


def test_the_opener_stays_inside_the_row_it_opens(page):
    """It shares the gutter's middle slot with the tag stripes, and grid
    auto-placement is sparse: an item that names column 2 after one that
    names column 3 starts a new implicit ROW rather than backing up. Naming
    the column without the row put the glyph in a second 13px row of a 23px
    gutter, half of it hanging over the row below — where the pointer had
    already left this row, the glyph had gone invisible again, and the click
    landed on the next row's gutter and selected it."""
    row = page.locator("#body .row").nth(2)
    tracks = row.locator(".gutter").evaluate("(g) => getComputedStyle(g).gridTemplateRows").split()
    assert len(tracks) == 1, f"the gutter grew a second row: {tracks}"

    row.hover()
    rb = row.bounding_box()
    ob = row.locator(".row-open").bounding_box()
    assert ob["y"] >= rb["y"] - 1 and ob["y"] + ob["height"] <= rb["y"] + rb["height"] + 1, (rb, ob)
    # Slot 2, flush against the digits — not over them.
    assert ob["x"] + ob["width"] <= row.locator(".rid").bounding_box()["x"] + 1, ob
    # And the whole glyph is the target: what is under its middle is itself,
    # not the gutter of the row underneath.
    assert row.locator(".row-open").evaluate(
        """(o) => { const r = o.getBoundingClientRect();
             return document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2) === o; }""")


def test_the_opener_does_not_move_the_checkbox_or_the_digits(page):
    """`visibility: hidden` still takes layout, so a mislaid opener shifted
    the other two slots on every landed row whether it was hovered or not —
    and the header gutter, which has no opener, stopped lining up with them.
    That alignment is the whole reason the gutter is a grid (style.css)."""
    head_cb = page.locator("#selectAllRows").bounding_box()
    row = page.locator("#body .row").nth(2)
    rb = row.bounding_box()
    cb = row.locator(".rowcheck").bounding_box()
    assert abs(cb["x"] - head_cb["x"]) < 1.5, (head_cb, cb)
    for name, box in (("checkbox", cb), ("rid", row.locator(".rid").bounding_box())):
        assert abs((box["y"] + box["height"] / 2) - (rb["y"] + rb["height"] / 2)) < 1.5, (name, rb, box)


def test_a_row_still_paging_in_has_nothing_to_open(page):
    assert page.evaluate(
        """() => {
             const row = __winnow.buildDataRow(0, null, __winnow.rowPaintContext());
             return row.querySelectorAll('.row-open').length;
           }""") == 0


# ------------------------------------------------------- the one-time hint

def _hint(page):
    return page.locator("#rowOpenHint")


def test_the_hint_is_there_until_a_row_has_been_opened(page):
    assert _hint(page).is_visible()
    page.evaluate("() => __winnow.showDetail(0)")
    page.wait_for_selector("#rowOpenHint", state="hidden")


def test_it_does_not_come_back_on_the_next_load(page):
    page.evaluate("() => __winnow.showDetail(0)")
    page.wait_for_selector("#rowOpenHint", state="hidden")
    page.reload(wait_until="networkidle")
    page.wait_for_selector(".row")
    assert not _hint(page).is_visible()


# -------------------------------------------------------- the stats line

STATS = re.compile(r"^200 of 200 rows · \d+ ms$")


def _stats(page):
    return page.evaluate("() => document.getElementById('viewStats').textContent")


def test_the_line_says_what_is_on_screen_and_not_where_it_came_from(page):
    page.wait_for_function("() => /of 200 rows/.test(document.getElementById('viewStats').textContent)")
    assert STATS.match(_stats(page)), _stats(page)


def test_reopening_a_table_describes_it_the_same_way(page):
    """The cached path used to end the sentence with `cached` — the same
    200 rows described two different ways depending on whether the view
    happened to still be materialised."""
    page.evaluate("() => __winnow.openSource(__winnow.S.sourceId)")
    page.wait_for_function(
        "() => document.getElementById('viewStats').title !== ''")
    assert STATS.match(_stats(page)), _stats(page)
    assert "cached" not in _stats(page)
    # Kept, where something you go looking for belongs.
    assert "rebuilt" in page.locator("#viewStats").get_attribute("title")


# ------------------------------------------------------- the detail pane

def _open_row(page, pos=1):
    page.evaluate("(p) => __winnow.showDetail(p)", pos)
    page.wait_for_selector("#detail:not([hidden])")


def test_field_names_are_the_column_s_own_name(page):
    _open_row(page)
    dt = page.locator("#detailFields dt", has_text="CommandLine").first
    assert dt.inner_text() == "CommandLine"
    assert dt.evaluate("(n) => getComputedStyle(n).textTransform") == "none"


def test_every_field_offers_a_copy_of_its_own(page):
    _open_row(page)
    dds = page.locator("#detailFields dd")
    assert dds.count() >= 4
    assert page.locator("#detailFields dd .dfield-copy").count() == dds.count()


def test_the_copy_button_waits_for_the_pointer(page):
    _open_row(page)
    dd = page.locator("#detailFields dd").first
    btn = dd.locator(".dfield-copy")
    assert not btn.is_visible()
    dd.hover()
    assert btn.is_visible()


def test_it_copies_that_field_and_nothing_else(page):
    _open_row(page)
    dd = page.locator("#detailFields dd[data-col='CommandLine']")
    dd.hover()
    dd.locator(".dfield-copy").click()
    page.wait_for_timeout(300)
    clip = page.evaluate("() => navigator.clipboard.readText()")
    assert clip == _raw(page, 1, "CommandLine")
    assert "Timestamp" not in clip, "that is Copy row's job"


def test_the_copy_follows_the_column_s_display_format(page):
    """The same rule the grid's copies follow (test_copy_formatted.py):
    what is on the clipboard is what was on the screen."""
    page.evaluate("""() => {
      __winnow.S.layout['Timestamp'] = Object.assign({}, __winnow.S.layout['Timestamp'] || {}, { tsFormat: 'us' });
    }""")
    _open_row(page)
    dd = page.locator("#detailFields dd[data-col='Timestamp']")
    assert "03/14/2026" in dd.inner_text()
    dd.hover()
    dd.locator(".dfield-copy").click()
    page.wait_for_timeout(300)
    assert page.evaluate("() => navigator.clipboard.readText()").startswith("03/14/2026")
