"""Two settings that had only ever been set by assigning S.appearance in a
test, never through Settings: Appearance → Row density, and Grid & layout →
Autofit column width limit.

Density changes every pixel position the grid has: the spacer's height, the
scroll offset, each row's place. What it owes the analyst is that the rows
change height and the row they were reading stays at the top, rather than
the view jumping to wherever the old pixel offset now falls. The width
limit's box clamps what is typed into it, and "No limit" is stored as 0,
which is a different thing from no setting at all.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui

ROW_H = "() => parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--row-h'))"

# The row at the top of the grid: the anchor a density change keeps.
TOP_ROW = """() => {
  const b = document.getElementById('body');
  const rh = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--row-h'));
  const total = __winnow.S.groupByCols.length ? __winnow.S.groupTotalRows : __winnow.S.view.row_count;
  return Math.floor(__winnow.vScroll(b, total, __winnow.headH(), rh) / rh);
}"""

PAINTED_ROW_H = """() => {
  const r = document.querySelector('#body .row');
  return r ? r.getBoundingClientRect().height : null;
}"""


def _section(page, name):
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modalBody .settings-nav-item", has_text=re.compile(rf"^{re.escape(name)}$")).click()
    return page.locator("#modalBody .settings-pane:not([hidden])")


def _density_button(pane, label):
    return pane.locator(".segmented button", has_text=re.compile(rf"^{label}$"))


def _stored(page, key):
    return page.evaluate("(k) => JSON.parse(localStorage.getItem('winnow.appearance') || '{}')[k]", key)


@pytest.fixture
def comfortable(page):
    """Starts and ends on the shared page's density, ungrouped and at the top."""
    page.wait_for_selector("#body .row")
    assert page.evaluate(ROW_H) == 24
    try:
        yield
    finally:
        page.evaluate("""async () => {
          if (!document.getElementById('modal').hidden) __winnow.closeModal();
          __winnow.applyDensity('comfortable');
          if (__winnow.S.groupByCols.length) await __winnow.dropGrouping();
          document.getElementById('body').scrollTop = 0;
        }""")
        page.wait_for_function("() => __winnow.busyCount === 0")


def test_compact_rows_are_shorter_and_the_top_row_stays_put(page, comfortable):
    page.evaluate("() => { const b = document.getElementById('body'); b.scrollTop = 60 * 24; }")
    page.wait_for_function(f"() => ({TOP_ROW})() === 60")
    pane = _section(page, "Appearance")
    assert _density_button(pane, "Comfortable").get_attribute("aria-pressed") == "true"

    _density_button(pane, "Compact").click()
    assert _density_button(pane, "Compact").get_attribute("aria-pressed") == "true"
    assert _density_button(pane, "Comfortable").get_attribute("aria-pressed") == "false"
    assert page.evaluate(ROW_H) == 20
    page.wait_for_function(f"() => ({PAINTED_ROW_H})() === 20")
    assert page.evaluate(TOP_ROW) == 60, "the view moved off the row that was at the top"
    # The row painted at the top of the body is that row.
    top = page.evaluate("""() => {
      const head = document.getElementById('gridHead').getBoundingClientRect().bottom;
      const rows = [...document.querySelectorAll('#body .row')]
        .filter((r) => r.getBoundingClientRect().bottom > head + 1);
      return rows.length ? Number(rows[0].dataset.pos) : null;
    }""")
    assert top == 60
    assert _stored(page, "density") == "compact"

    _density_button(pane, "Comfortable").click()
    assert page.evaluate(ROW_H) == 24
    page.wait_for_function(f"() => ({PAINTED_ROW_H})() === 24")
    assert page.evaluate(TOP_ROW) == 60
    assert _stored(page, "density") == "comfortable"


def test_density_changes_a_grouped_view_too(page, comfortable):
    """A grouped grid keeps its own row count (group headers and their
    rows) and prefix of positions, rebuilt against the new height."""
    page.evaluate("() => __winnow.addGroupLevel('Host')")
    page.wait_for_function("() => __winnow.S.groupByCols.length === 1 && __winnow.busyCount === 0")
    page.wait_for_selector("#body .row")
    pane = _section(page, "Appearance")
    _density_button(pane, "Compact").click()
    page.wait_for_function(f"() => ({PAINTED_ROW_H})() === 20")
    rows = page.evaluate("""() => [...document.querySelectorAll('#body .row')]
      .map((r) => Math.round(r.getBoundingClientRect().height))""")
    assert rows and set(rows) == {20}, rows


@pytest.fixture
def default_cap(page):
    page.wait_for_selector("#body .row")
    before = page.evaluate("() => __winnow.S.appearance.autofitMax")
    try:
        yield
    finally:
        page.evaluate("""(v) => {
          if (!document.getElementById('modal').hidden) __winnow.closeModal();
          __winnow.S.appearance.autofitMax = v; __winnow.saveAppearance();
        }""", before)


def _cap_controls(page):
    pane = _section(page, "Grid & layout")
    box = pane.locator("input[type=number]")
    no_limit = pane.locator("label.check-row", has_text="No limit").locator("input")
    box.wait_for(state="visible")
    return box, no_limit


def _set_cap(box, value):
    box.fill(str(value))
    box.dispatch_event("change")


def test_the_width_limit_box_clamps_and_saves_what_is_typed(page, default_cap):
    box, no_limit = _cap_controls(page)
    assert not no_limit.is_checked() and box.is_enabled()

    _set_cap(box, 400)
    assert page.evaluate("() => __winnow.S.appearance.autofitMax") == 400
    assert _stored(page, "autofitMax") == 400

    # Below the floor, above the ceiling, and not a number at all.
    for typed, kept in ((10, 80), (999999, 20000), ("", 900)):
        _set_cap(box, typed)
        assert box.input_value() == str(kept), typed
        assert _stored(page, "autofitMax") == kept, typed


def test_no_limit_is_stored_as_zero_and_turns_the_box_off(page, default_cap):
    box, no_limit = _cap_controls(page)
    _set_cap(box, 600)

    no_limit.check()
    assert box.is_disabled()
    assert _stored(page, "autofitMax") == 0
    assert page.evaluate("() => __winnow.autofitMaxWidth()") == 0

    # Off again: the limit is the one in the box, not the default.
    no_limit.uncheck()
    assert box.is_enabled()
    assert _stored(page, "autofitMax") == 600

    # Settings opens again on what was left: no limit, the box off.
    no_limit.check()
    page.evaluate("() => __winnow.closeModal()")
    box, no_limit = _cap_controls(page)
    assert no_limit.is_checked() and box.is_disabled()
    assert box.input_value() == "900"
