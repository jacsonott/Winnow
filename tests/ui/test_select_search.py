"""Type to search in a drop-down list (static/js/selectsearch.js) and in a
drop-down menu (ui.js menuFinder).

What was asked: every drop-down with more than five options can be
searched, and the Add derived column dialog first, where the Parse column
and Format lists run to dozens of entries and the browser's own list could
only jump to the first option STARTING with a typed letter. These drive
the real gestures — a press on the select, keys typed into it — rather
than select_option, which sets the value without ever opening a list and
so would pass whether or not anything here worked."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

POP = ".select-search"
COLUMN = "#modalBody select[data-role=column]"
FORMAT = '#modalBody select[data-role="format"]'


def _open_dialog(page, format_detected, column="Host"):
    page.evaluate(f"() => __winnow.openDerivedColumnModal({column!r})")
    page.wait_for_selector("#modal:not([hidden])")
    format_detected()


def _options(page):
    return page.locator(f"{POP} .select-search-option").all_inner_texts()


def test_a_press_on_the_parse_column_list_opens_a_search_and_enter_takes_the_top_hit(page, format_detected):
    _open_dialog(page, format_detected)
    sel = page.locator(COLUMN)
    sel.click()
    page.wait_for_selector(POP)
    # The caret is in the search box, and every column is offered.
    assert page.evaluate("() => document.activeElement.classList.contains('select-search-box')")
    assert len(_options(page)) == 6
    # The option the select stands on is the one marked.
    assert page.locator(f"{POP} .select-search-option.current").inner_text() == "Host"
    # Contains, not starts with: "line" finds CommandLine.
    page.keyboard.type("line")
    assert _options(page) == ["CommandLine"]
    assert page.locator(f"{POP} .select-search-hit").inner_text().lower() == "line"
    page.keyboard.press("Enter")
    page.wait_for_selector(POP, state="detached")
    assert sel.input_value() == "CommandLine"
    # The change reached the dialog: its name suggestion follows the column.
    page.wait_for_function("() => document.querySelector('.derived-name').value.startsWith('CommandLine')")
    # And the focus is back on the select, as the native list leaves it.
    assert page.evaluate("() => document.activeElement.dataset.role") == "column"
    assert not page.locator("#modal").is_hidden()


def test_every_word_has_to_match_in_any_order(page, format_detected):
    _open_dialog(page, format_detected)
    page.locator(COLUMN).click()
    page.wait_for_selector(POP)
    page.keyboard.type("header long")
    assert _options(page) == ["ExtremelyLongColumnHeaderName"]
    page.keyboard.press("Control+a")
    page.keyboard.type("nothing like this")
    assert _options(page) == []
    assert "No option matches" in page.locator(f"{POP} .select-search-note").inner_text()
    # Enter with nothing to take changes nothing and keeps the list up.
    page.keyboard.press("Enter")
    assert page.locator(POP).is_visible()
    assert page.locator(COLUMN).input_value() == "Host"


def test_escape_closes_the_list_and_not_the_dialog(page, format_detected):
    _open_dialog(page, format_detected)
    page.locator(COLUMN).click()
    page.wait_for_selector(POP)
    page.keyboard.type("payload")
    page.keyboard.press("Escape")
    page.wait_for_selector(POP, state="detached")
    assert not page.locator("#modal").is_hidden()
    assert page.locator(COLUMN).input_value() == "Host"


def test_a_click_on_an_option_picks_it_and_a_second_press_on_the_select_closes_it(page, format_detected):
    _open_dialog(page, format_detected)
    sel = page.locator(COLUMN)
    sel.click()
    page.wait_for_selector(POP)
    sel.click()
    page.wait_for_selector(POP, state="detached")
    assert sel.input_value() == "Host"
    sel.click()
    page.locator(f"{POP} .select-search-option", has_text="EventId").click()
    page.wait_for_selector(POP, state="detached")
    assert sel.input_value() == "EventId"
    assert not page.locator("#modal").is_hidden()


def test_typing_on_a_focused_select_starts_the_search_with_that_letter(page, format_detected):
    _open_dialog(page, format_detected)
    page.locator(COLUMN).focus()
    page.keyboard.press("p")
    page.wait_for_selector(POP)
    assert page.locator(f"{POP} .select-search-box").input_value() == "p"
    # The columns CONTAINING a p, in their own order — the native list
    # would have jumped straight to Payload and fired its change.
    assert _options(page) == ["Timestamp", "Payload"]
    assert page.locator(COLUMN).input_value() == "Host"
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.wait_for_selector(POP, state="detached")
    assert page.locator(COLUMN).input_value() == "Payload"


def test_alt_down_opens_the_list_and_the_arrows_walk_it(page, format_detected):
    _open_dialog(page, format_detected)
    page.locator(COLUMN).focus()
    page.keyboard.press("Alt+ArrowDown")
    page.wait_for_selector(POP)
    # From Host, down one is the column after it.
    page.keyboard.press("ArrowDown")
    assert page.locator(f"{POP} .select-search-option.active").inner_text() == "ExtremelyLongColumnHeaderName"
    page.keyboard.press("Enter")
    page.wait_for_selector(POP, state="detached")
    assert page.locator(COLUMN).input_value() == "ExtremelyLongColumnHeaderName"


def test_the_format_list_searches_its_groups_too(page, format_detected, derive_pick):
    _open_dialog(page, format_detected, column="Timestamp")
    derive_pick("Parse a timestamp")
    fmt = page.locator(FORMAT)
    before = fmt.input_value()
    fmt.click()
    page.wait_for_selector(POP)
    # The optgroup labels head their options in the list.
    assert page.locator(f"{POP} .select-search-group").first.inner_text().strip().lower() == "built in"
    page.keyboard.type("filetime")
    hits = _options(page)
    assert hits and all("filetime" in h.lower() for h in hits), hits
    page.keyboard.press("Enter")
    page.wait_for_selector(POP, state="detached")
    assert fmt.input_value() != before
    assert "filetime" in fmt.input_value().lower()


def test_a_list_of_five_or_fewer_keeps_the_browsers_own(page, format_detected):
    _open_dialog(page, format_detected)
    # A select of three, made the way the app makes them.
    page.evaluate("""() => {
      const s = document.createElement('select');
      s.id = 'shortList';
      for (const v of ['one', 'two', 'three']) s.append(new Option(v, v));
      document.getElementById('modalBody').prepend(s);
    }""")
    page.locator("#shortList").click()
    # Nothing to wait for: the check is that no list of ours appeared.
    page.wait_for_timeout(200)
    assert page.locator(POP).count() == 0
    assert page.evaluate("() => __winnow.isSearchable(document.getElementById('shortList'))") is False
    page.keyboard.press("Escape")


def test_a_select_inside_an_open_panel_keeps_the_panel(page):
    # A select in an anchoredPanel — a .menu like the one the list itself
    # is — must not have its panel closed by the list opening, or by a
    # press inside the list, which is outside the panel.
    page.evaluate("""() => {
      const b = document.createElement('button');
      b.id = 'panelAnchor';
      b.textContent = 'panel';
      Object.assign(b.style, { position: 'fixed', top: '120px', left: '120px', zIndex: 60 });
      document.body.append(b);
      __winnow.anchoredPanel(b, 'test-panel', (p) => {
        const s = document.createElement('select');
        s.id = 'panelSelect';
        for (const v of ['a1', 'b2', 'c3', 'd4', 'e5', 'f6', 'g7']) s.append(new Option(v, v));
        p.append(s);
      });
    }""")
    try:
        page.wait_for_selector(".test-panel #panelSelect")
        page.locator("#panelSelect").click()
        page.wait_for_selector(POP)
        page.locator(f"{POP} .select-search-option", has_text="f6").click()
        page.wait_for_selector(POP, state="detached")
        assert page.locator(".test-panel").is_visible()
        assert page.locator("#panelSelect").input_value() == "f6"
    finally:
        page.evaluate("() => { __winnow.closeMenu(); document.getElementById('panelAnchor').remove(); }")


def test_a_dropdown_menu_of_more_than_five_gets_a_search_box(page):
    page.evaluate("""() => {
      window.__picked = null;
      const b = document.createElement('button');
      b.id = 'menuAnchor';
      b.textContent = 'menu';
      Object.assign(b.style, { position: 'fixed', top: '120px', left: '120px', zIndex: 60 });
      document.body.append(b);
      const items = [{ header: 'Tables' }];
      for (const n of ['Security.evtx', 'System.evtx', 'Application.evtx', '$MFT', 'Amcache', 'Prefetch'])
        items.push({ label: n, onclick: () => { window.__picked = n; } });
      items.push('-', { header: 'Other' }, { label: 'Decode', submenu: [{ label: 'Base64', onclick() {} }] });
      b.onclick = () => __winnow.dropdownMenu(b, items);
    }""")
    try:
        page.locator("#menuAnchor").click()
        box = page.locator(".menu .menu-find")
        box.wait_for()
        assert page.evaluate("() => document.activeElement.classList.contains('menu-find')")
        page.keyboard.type("evtx sys")
        shown = page.locator(".menu > .menu-item:visible").all_inner_texts()
        assert [s.strip() for s in shown] == ["System.evtx"]
        # A header with nothing left under it steps aside; one with a hit stays.
        headers = page.locator(".menu > .menu-header:visible").all_inner_texts()
        assert [h.lower() for h in headers] == ["tables"]
        # A word inside a submenu keeps the item that leads to it.
        box.fill("base64")
        shown = page.locator(".menu > .menu-item:visible").all_inner_texts()
        assert len(shown) == 1 and shown[0].startswith("Decode")
        box.fill("amc")
        page.keyboard.press("Enter")
        page.wait_for_selector(".menu", state="detached")
        assert page.evaluate("() => window.__picked") == "Amcache"
        # Down steps from the box into what is left; Escape gives the
        # focus back to the button the menu took it from.
        page.locator("#menuAnchor").click()
        box.wait_for()
        page.keyboard.type("evtx")
        page.keyboard.press("ArrowDown")
        assert page.evaluate("() => document.activeElement.textContent.trim()") == "Security.evtx"
        page.keyboard.press("Escape")
        page.wait_for_selector(".menu", state="detached")
        assert page.evaluate("() => document.activeElement.id") == "menuAnchor"
    finally:
        page.evaluate("() => { __winnow.closeMenu(); document.getElementById('menuAnchor').remove(); }")


def test_a_dropdown_menu_of_five_has_no_search_box(page):
    page.evaluate("""() => {
      const b = document.createElement('button');
      b.id = 'menuAnchor';
      b.textContent = 'menu';
      Object.assign(b.style, { position: 'fixed', top: '120px', left: '120px', zIndex: 60 });
      document.body.append(b);
      b.onclick = () => __winnow.dropdownMenu(b, ['a', 'b', 'c', 'd', 'e'].map((l) => ({ label: l, onclick() {} })));
    }""")
    try:
        page.locator("#menuAnchor").click()
        page.wait_for_selector(".menu .menu-item")
        assert page.locator(".menu .menu-find").count() == 0
    finally:
        page.evaluate("() => { __winnow.closeMenu(); document.getElementById('menuAnchor').remove(); }")
