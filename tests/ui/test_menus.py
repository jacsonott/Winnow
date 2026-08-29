"""The right-click surfaces and the header value picker.

Smoke-level on purpose: these assert that each surface opens where it should,
carries the entries it's supposed to, and that the one that writes a filter
writes one the grid actually applies. The value picker's *edge* semantics
(values containing `|`, edge whitespace, `(empty)` mixed with real values)
are worth pinning too, because they route through the filter tree instead of
the header box and nothing else in the suite would notice them breaking.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def open_row_menu(page, row=2, cell=2):
    page.locator(".row").nth(row).locator(".cell").nth(cell).click(button="right")
    page.wait_for_selector(".menu")


def test_row_right_click_menu_has_tags_and_cell_actions(page):
    open_row_menu(page)
    items = page.locator(".menu .menu-item").all_inner_texts()
    joined = " | ".join(i.replace("\n", " ") for i in items)
    assert "Edit tags…" in joined
    assert "Filter to" in joined and "Exclude" in joined
    assert "Copy cell" in joined and "Copy row" in joined
    # Tag rows carry their swatch and hotkey hint — the menu is the discoverable
    # copy of the 1-9 hotkeys, so losing them makes it a worse menu, not a
    # broken one, and nothing else would catch it.
    assert page.locator(".menu .menu-swatch").count() >= 1


def test_row_menu_scope_follows_the_selection(page):
    for i in (1, 2, 3):
        page.locator(".row").nth(i).locator(".rowcheck").check()
    open_row_menu(page, row=2)
    assert "3 SELECTED ROWS" in page.locator(".menu .menu-header").first.inner_text().upper()
    page.keyboard.press("Escape")
    # Escape closes the menu without discarding the selection underneath it.
    assert page.locator(".menu").count() == 0
    assert page.locator(".row.selected").count() == 3

    # Right-clicking outside the selection collapses onto that one row.
    open_row_menu(page, row=9)
    assert "THIS ROW" in page.locator(".menu .menu-header").first.inner_text().upper()


def test_row_menu_filters_by_the_clicked_cell(page):
    host_index = page.evaluate("() => __winnow.visibleCols().indexOf('Host')")
    value = page.locator(".row").first.locator(".cell").nth(host_index).inner_text()
    page.locator(".row").first.locator(".cell").nth(host_index).click(button="right")
    page.wait_for_selector(".menu")
    page.click(f".menu .menu-item:has-text('Filter to {value}')")
    page.wait_for_timeout(800)
    assert page.locator('.fcell input[data-col="Host"]').input_value() == f"={value}"
    assert page.evaluate("() => __winnow.S.view.row_count") < 200


def test_column_header_right_click_opens_column_options(page):
    page.locator('.hcell[data-col="Timestamp"]').click(button="right")
    page.wait_for_selector(".menu")
    items = " | ".join(page.locator(".menu .menu-item").all_inner_texts())
    assert "Add datetime column from this…" in items
    assert "YYYY-MM-DD HH:MM:SS" in items  # display formats, for a datetime column
    assert page.locator(".hcell-fmt").count() == 0, "the ▾ button was replaced by this menu"


def test_tab_right_click_opens_the_table_menu(page):
    page.locator("#sourceTabs .tab").first.click(button="right")
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").inner_text().upper().startswith("TABLE —")
    assert page.locator(".table-menu-section h4").count() >= 3


def test_value_picker_writes_a_filter_the_grid_applies(page):
    page.click('.fcell-pick[data-col="EventId"]')
    page.wait_for_selector(".value-picker .vp-row")
    assert page.locator(".value-picker .vp-row").count() == 4  # the fixture's four EventIds
    page.click(".vp-actions .btn:has-text('None')")
    page.locator(".value-picker .vp-row input").nth(0).check()
    page.locator(".value-picker .vp-row input").nth(1).check()
    page.click(".vp-actions .btn:has-text('Apply')")
    page.wait_for_timeout(900)
    raw = page.locator('.fcell input[data-col="EventId"]').input_value()
    assert "|" in raw, f"two ticked values should become an any-of filter, got {raw!r}"
    assert page.evaluate("() => __winnow.S.view.row_count") == 100  # two of four, 50 rows each

    # Ticking everything back is the same statement as no filter, and says so
    # rather than writing a filter listing every value in the column.
    page.click('.fcell-pick[data-col="EventId"]')
    page.wait_for_selector(".value-picker .vp-row")
    page.click(".vp-actions .btn:has-text('All')")
    page.click(".vp-actions .btn:has-text('Apply')")
    page.wait_for_timeout(900)
    assert page.locator('.fcell input[data-col="EventId"]').input_value() == ""
    assert page.evaluate("() => __winnow.S.view.row_count") == 200


def test_value_picker_routes_unspellable_values_to_the_filter_tree(page):
    """A value containing `|` can't go in the header box as part of an any-of
    list — the box would read it as a separator. Those selections become a
    condition in the guided filter tree instead, and the picker's own button
    marks itself so the column doesn't look unfiltered."""
    # Driven through applyValueSelection directly rather than by ticking boxes:
    # what's under test is what the picker does with a selection it can't
    # spell, not whether this fixture happens to contain a pipe.
    applied = page.evaluate("""async () => {
      await __winnow.applyValueSelection('Host', ['H1|x', 'H2'], { clearInstead: false });
      return {
        box: __winnow.S.filters['Host'] || '',
        tree: JSON.stringify(__winnow.S.filterTree),
        marked: !!document.querySelector('.fcell-pick[data-col="Host"].active'),
      };
    }""")
    assert applied["box"] == "", "a pipe-carrying selection must not be written into the box"
    assert '"op":"in"' in applied["tree"] and "H1|x" in applied["tree"]
    assert applied["marked"], "the column's picker button should show the filter lives in the tree"


def test_settings_sections_start_collapsed_and_expand_on_click(page):
    page.keyboard.press("?")
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator(".settings-section-head").count() >= 6
    assert page.locator(".settings-section-body:not([hidden])").count() == 0
    page.click(".settings-section-head:has-text('Appearance')")
    page.wait_for_timeout(200)
    assert page.locator(".settings-section-body:not([hidden])").count() == 1
    assert page.locator(".style-card").first.is_visible()
