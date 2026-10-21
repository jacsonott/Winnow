"""The table menu's side list, and the sections behind it that no older
test reaches.

Two panes, the shape of Settings: the sections down the left with what
each holds under its name, the chosen one on the right. The side list is
walked from the keyboard; Value dropdowns sets each column's ▾ Auto, On
or Off; Name & merge saves the names as they are typed, the way the tag
editor does. Layout has test_apply_default_layout.py and
test_copy_layout_from_table.py."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open(page, section="columns"):
    page.evaluate("(s) => __winnow.openTableMenu(undefined, s)", section)
    page.wait_for_selector(f'.table-menu-pane[data-section="{section}"]')


def _hint(page, section):
    return page.locator(f'.side-rail-item[data-section="{section}"] .side-rail-hint').inner_text()


def _focused_section(page):
    return page.evaluate("() => document.activeElement.dataset.section || null")


def test_the_side_list_is_walked_with_the_arrow_keys(page):
    _open(page)
    # The caret starts on the side list, not in a box, so the arrows work
    # straight away (and C still closes the menu, test_modal_toggle.py).
    assert _focused_section(page) == "columns"
    assert "shown" in _hint(page, "columns")
    page.keyboard.press("ArrowDown")
    page.wait_for_selector('.table-menu-pane[data-section="valueFilters"]')
    assert _focused_section(page) == "valueFilters"
    page.keyboard.press("End")
    page.wait_for_selector('.table-menu-pane[data-section="name"]')
    page.keyboard.press("ArrowDown")
    page.wait_for_selector('.table-menu-pane[data-section="columns"]')
    assert _focused_section(page) == "columns", "Down from the last section wraps to the first"
    page.keyboard.press("ArrowUp")
    page.wait_for_selector('.table-menu-pane[data-section="name"]')


def test_a_column_set_by_hand_overrides_the_table_until_set_back_to_auto(page, layout_saved):
    _open(page, "valueFilters")
    assert page.evaluate("() => __winnow.S.valueFilterMode") == "auto"
    # 200 rows: Auto is on here, so every column has its ▾ to begin with.
    row = page.locator('.tm-vf-row[data-col="Host"]')
    assert row.locator("button[data-key=auto]").inner_text() == "Auto · on"
    assert page.locator('.fcell-pick[data-col="Host"]').count() == 1
    try:
        row.locator("button[data-key=off]").click()
        assert page.evaluate("() => __winnow.S.layout.Host.valuePicker") is False
        page.wait_for_selector('.fcell-pick[data-col="Host"]', state="detached")
        assert "set" in row.get_attribute("class").split()
        assert row.locator("button[data-key=off]").get_attribute("aria-pressed") == "true"
        assert "1 set by hand" in _hint(page, "valueFilters")
        assert "1 of them set by hand" in page.locator(".tm-vf-foot").inner_text()

        page.locator(".tm-vf-foot .btn", has_text="Set back to Auto").click()
        assert page.evaluate("() => 'valuePicker' in (__winnow.S.layout.Host || {})") is False
        page.wait_for_selector('.fcell-pick[data-col="Host"]')
        assert "set" not in row.get_attribute("class").split()
        assert "set by hand" not in _hint(page, "valueFilters")
    finally:
        page.evaluate("() => __winnow.setColumnValueFilter('Host', null)")
        layout_saved()


def test_the_nickname_saves_as_typed_and_escape_takes_back_an_edit(page, api):
    _open(page, "name")
    sid = page.evaluate("() => __winnow.S.sourceId")
    nickname = lambda: next(s for s in api("/api/sources") if s["id"] == sid).get("nickname") or ""
    box = page.locator(".tm-field", has_text="Nickname").locator("input")
    try:
        box.fill("Section test table")
        with page.expect_response(lambda r: r.url.endswith(f"/api/source/{sid}/nickname")):
            box.press("Enter")
        page.wait_for_function("() => /Section test table/i.test(document.querySelector('#modalTitle').textContent)")
        assert nickname() == "Section test table"
        page.wait_for_function("() => /^Nicknamed/.test(document.querySelector("
                               "'.side-rail-item[data-section=name] .side-rail-hint').textContent)")

        # Escape puts back what was saved and keeps the menu open.
        box.fill("Not this")
        box.press("Escape")
        assert box.input_value() == "Section test table"
        assert not page.locator("#modal").is_hidden()
        assert nickname() == "Section test table"

        # Blank goes back to the file name.
        box.fill("")
        with page.expect_response(lambda r: r.url.endswith(f"/api/source/{sid}/nickname")):
            box.press("Enter")
        assert nickname() == ""
        page.wait_for_function("() => !/Section test table/i.test(document.querySelector('#modalTitle').textContent)")
    finally:
        api(f"/api/source/{sid}/nickname", "POST", {"nickname": ""})
        page.evaluate("() => __winnow.loadSources()")


def test_the_header_set_name_saves_on_leaving_the_box(page, api):
    _open(page, "name")
    cols = page.evaluate("() => __winnow.S.columns.filter((c) => !c.derived).map((c) => c.name)")
    box = page.locator(".tm-field", has_text=f"These {len(cols)} columns").locator("input")
    before = box.input_value()
    try:
        box.fill("Section test headers")
        with page.expect_response(lambda r: "/api/header_nicknames" in r.url and r.request.method != "GET"):
            box.blur()
        page.wait_for_function("(c) => __winnow.nicknameFor(c) === 'Section test headers'", arg=cols)
        page.wait_for_selector(".tm-field .tm-saved:text('Saved')")
    finally:
        page.evaluate("([c, n]) => __winnow.saveHeaderNickname(c, n)", [cols, before])
