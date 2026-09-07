"""The row menu, one level deep at the top. The clicked column's filters
stay broken out; Tag, Add to dashboard, Copy and Plugins fold into
submenus that open on click (and on hover). Items in a submenu that
declare a pin id can be starred or dragged onto the top of the menu,
where they stay across reopens and reloads, per machine — and vanish
quietly while their plugin is off."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

FAKE = ("() => { __winnow.S.pluginRowActions = [{ id: 'demo.vt', local_id: 'vt', plugin: 'demo', "
        "plugin_fs: 'demo', label: 'Look up on VT', description: 'demo', max_rows: 50 }]; }")


def _open(page, row=1):
    page.locator(".row").nth(row).locator(".cell").nth(1).click(button="right")
    page.wait_for_selector(".menu:not(.menu-sub)")


def _sub(page, label):
    page.locator(".menu:not(.menu-sub) .menu-item-sub", has_text=label).click()
    page.wait_for_selector(".menu-sub")
    return page.locator(".menu-sub")


def _root_text(page):
    return page.locator(".menu:not(.menu-sub)").inner_text()


def _clear_pins(page):
    page.evaluate("() => localStorage.removeItem('winnow.menupins')")


def test_top_level_is_short_and_keeps_the_filters_broken_out(page):
    _open(page)
    labels = [t.replace("\n", " ") for t in page.locator(".menu:not(.menu-sub) .menu-item").all_inner_texts()]
    subs = [t.replace("\n", " ") for t in page.locator(".menu:not(.menu-sub) .menu-item-sub").all_inner_texts()]
    assert any(l.startswith("Tag this row") for l in subs)
    assert any(l.startswith("Add to dashboard") for l in subs)
    assert any(l.startswith("Copy") for l in subs)
    # the filters are plain items at the top, not folded
    assert any(l.startswith("Filter to") for l in labels) and any(l.startswith("Exclude") for l in labels)
    assert len(labels) <= 8, labels
    page.keyboard.press("Escape")


def test_tag_submenu_toggles_in_place_and_keeps_its_hotkeys(page):
    _open(page, row=3)
    sub = _sub(page, "Tag this row")
    first = sub.locator(".menu-item").first
    name = first.locator(".menu-item-text").inner_text()
    assert first.locator(".menu-swatch").count() == 1 and first.locator(".menu-item-hint").inner_text() == "1"
    assert first.locator(".menu-check").inner_text() == ""
    first.click()
    page.wait_for_function("() => (__winnow.rowAt(3) || { tags: [] }).tags.length === 1")
    # keepOpen: the flyout repainted rather than closing, with the ✓ on
    page.wait_for_selector(".menu-sub .menu-item .menu-check:has-text('✓')")
    assert page.locator(".menu-sub .menu-item").first.locator(".menu-check").inner_text() == "✓"
    page.locator(".menu-sub .menu-item").first.click()
    page.wait_for_function("() => (__winnow.rowAt(3) || { tags: [] }).tags.length === 0")
    page.keyboard.press("Escape")


def test_hovering_a_sibling_closes_the_flyout(page):
    _open(page)
    _sub(page, "Copy")
    page.locator(".menu:not(.menu-sub) .menu-item", has_text="Filter to").first.hover()
    page.wait_for_selector(".menu-sub", state="detached")
    page.keyboard.press("Escape")


def test_starring_a_plugin_action_pins_it_and_it_survives_a_reload(page):
    page.evaluate(FAKE)
    _clear_pins(page)
    try:
        _open(page)
        assert "Pinned" not in _root_text(page)
        sub = _sub(page, "Plugins")
        row = sub.locator(".menu-pinnable", has_text="Look up on VT")
        row.hover()
        row.locator(".menu-pin-btn").click()
        # the flyout closes, the root repaints with a Pinned section on top
        page.wait_for_selector(".menu-sub", state="detached")
        page.wait_for_selector(".menu:not(.menu-sub) .menu-header:has-text('Pinned')")
        pinned = page.locator(".menu:not(.menu-sub) .menu-pinnable", has_text="Look up on VT")
        assert pinned.count() == 1
        assert page.locator(".menu:not(.menu-sub) .menu-header").first.inner_text().lower() == "pinned"
        page.keyboard.press("Escape")

        # persisted: a fresh open, and a fresh page, still have it
        _open(page, row=4)
        assert page.locator(".menu:not(.menu-sub) .menu-pinnable", has_text="Look up on VT").count() == 1
        page.keyboard.press("Escape")
        page.reload(wait_until="networkidle")
        page.wait_for_selector(".row")
        page.evaluate(FAKE)
        _open(page)
        assert page.locator(".menu:not(.menu-sub) .menu-pinnable", has_text="Look up on VT").count() == 1
        # ✕ unpins
        page.locator(".menu:not(.menu-sub) .menu-pinnable", has_text="Look up on VT").locator(".menu-pin-btn").click()
        page.wait_for_function("() => !document.querySelector('.menu:not(.menu-sub) .menu-pinnable')")
        assert "Look up on VT" not in _root_text(page)
        page.keyboard.press("Escape")
    finally:
        _clear_pins(page)
        page.evaluate("() => { __winnow.S.pluginRowActions = []; }")


def test_dragging_out_of_a_submenu_pins_it(page):
    page.evaluate(FAKE)
    _clear_pins(page)
    try:
        _open(page)
        sub = _sub(page, "Plugins")
        item = sub.locator(".menu-pinnable .menu-item", has_text="Look up on VT")
        assert item.get_attribute("draggable") == "true"
        item.drag_to(page.locator(".menu:not(.menu-sub) .menu-item-sub", has_text="Copy"))
        page.wait_for_selector(".menu:not(.menu-sub) .menu-header:has-text('Pinned')")
        assert page.locator(".menu:not(.menu-sub) .menu-pinnable", has_text="Look up on VT").count() == 1
        page.keyboard.press("Escape")
    finally:
        _clear_pins(page)
        page.evaluate("() => { __winnow.S.pluginRowActions = []; }")


def test_a_pinned_action_runs_and_hides_while_its_plugin_is_off(page):
    page.evaluate(FAKE)
    page.evaluate("() => { localStorage.setItem('winnow.menupins', JSON.stringify({ row: ['plugin:demo:vt', 'copy:cell'] })); }")
    try:
        _open(page)
        top = page.locator(".menu:not(.menu-sub)")
        assert top.locator(".menu-pinnable", has_text="Look up on VT").count() == 1
        assert top.locator(".menu-pinnable", has_text="Copy cell").count() == 1
        page.keyboard.press("Escape")
        # plugin off: its pin is not shown, the other pin still is
        page.evaluate("() => { __winnow.S.pluginRowActions = []; }")
        _open(page)
        assert top.locator(".menu-pinnable", has_text="Look up on VT").count() == 0
        assert top.locator(".menu-pinnable", has_text="Copy cell").count() == 1
        # the pin survives for when the plugin returns
        assert "plugin:demo:vt" in page.evaluate("() => localStorage.getItem('winnow.menupins')")
        # a pinned copy action still does its job
        page.locator(".menu:not(.menu-sub) .menu-pinnable .menu-item", has_text="Copy cell").click()
        page.wait_for_selector(".menu", state="detached")
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:   # evaluate awaits the promise; wait_for_function would not
            if page.evaluate("() => navigator.clipboard.readText().then((t) => t.length > 0)"):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("nothing reached the clipboard")
    finally:
        _clear_pins(page)
        page.evaluate("() => { __winnow.S.pluginRowActions = []; }")


def test_pinned_tag_toggles_from_the_top_level(page):
    # Pins key on the tag's NAME: ids are per case file and get reused.
    tag_name = page.evaluate("() => __winnow.S.tags[0].name")
    page.evaluate("(n) => { localStorage.setItem('winnow.menupins', JSON.stringify({ row: ['tag:' + n] })); }", tag_name)
    try:
        _open(page, row=5)
        pinned = page.locator(".menu:not(.menu-sub) .menu-pinnable .menu-item").first
        assert pinned.locator(".menu-swatch").count() == 1
        pinned.click()
        page.wait_for_function("() => (__winnow.rowAt(5) || { tags: [] }).tags.length === 1")
        page.wait_for_selector(".menu:not(.menu-sub) .menu-pinnable .menu-check:has-text('✓')")   # keepOpen repaint
        page.locator(".menu:not(.menu-sub) .menu-pinnable .menu-item").first.click()
        page.wait_for_function("() => (__winnow.rowAt(5) || { tags: [] }).tags.length === 0")
        page.keyboard.press("Escape")
    finally:
        _clear_pins(page)


def test_hover_then_click_leaves_the_flyout_open(page):
    _open(page)
    parent = page.locator(".menu:not(.menu-sub) .menu-item-sub", has_text="Copy")
    parent.hover()
    page.wait_for_selector(".menu-sub")          # hover opened it
    parent.click()                                # the click must not toggle it shut
    page.wait_for_timeout(250)
    assert page.locator(".menu-sub").count() == 1
    assert parent.get_attribute("aria-expanded") == "true"
    page.keyboard.press("Escape")


def test_rules_sit_before_and_after_the_filter_block_only(page):
    _open(page)
    kinds = page.evaluate("""() => [...document.querySelector('.menu:not(.menu-sub)').children].map(n =>
        n.classList.contains('menu-sep') ? '-' : n.classList.contains('menu-header') ? 'H' :
        n.classList.contains('menu-dropzone') ? '' : (n.querySelector('.menu-item-text') || n).textContent.trim().split(' ')[0])""")
    kinds = [k for k in kinds if k]
    seps = [i for i, k in enumerate(kinds) if k == "-"]
    assert len(seps) == 2, kinds
    assert kinds[seps[0] + 1] == "H"                  # the column header opens the block
    assert kinds[seps[1] - 1].startswith("Filter")    # the last filter item closes it
    assert kinds[seps[1] + 1] == "Add"                # then the folded entries
    page.keyboard.press("Escape")


def test_undo_appears_at_the_top_after_a_pinned_tag(page):
    tag_name = page.evaluate("() => __winnow.S.tags[0].name")
    page.evaluate("(n) => { localStorage.setItem('winnow.menupins', JSON.stringify({ row: ['tag:' + n] })); }", tag_name)
    try:
        _open(page, row=6)
        page.locator(".menu:not(.menu-sub) .menu-pinnable .menu-item").first.click()
        page.wait_for_function("() => (__winnow.rowAt(6) || { tags: [] }).tags.length === 1")
        page.wait_for_selector(".menu:not(.menu-sub) .menu-item:has-text('Undo')")
        page.locator(".menu:not(.menu-sub) .menu-item", has_text="Undo").click()
        page.wait_for_function("() => (__winnow.rowAt(6) || { tags: [] }).tags.length === 0")
    finally:
        _clear_pins(page)


def test_pinned_tag_and_its_flyout_twin_agree(page):
    tag_name = page.evaluate("() => __winnow.S.tags[0].name")
    page.evaluate("(n) => { localStorage.setItem('winnow.menupins', JSON.stringify({ row: ['tag:' + n] })); }", tag_name)
    try:
        _open(page, row=7)
        sub = _sub(page, "Tag this row")
        sub.locator(".menu-item").first.click()          # toggle inside the flyout
        page.wait_for_function("() => (__winnow.rowAt(7) || { tags: [] }).tags.length === 1")
        # the flyout stayed open and the pinned twin at the top caught up
        page.wait_for_selector(".menu:not(.menu-sub) .menu-pinnable .menu-check:has-text('\u2713')")
        assert page.locator(".menu-sub .menu-item").first.locator(".menu-check").inner_text() == "\u2713"
        # and clicking the pinned twin now REMOVES rather than re-applying
        page.locator(".menu:not(.menu-sub) .menu-pinnable .menu-item").first.click()
        page.wait_for_function("() => (__winnow.rowAt(7) || { tags: [] }).tags.length === 0")
        page.keyboard.press("Escape")
    finally:
        _clear_pins(page)


def test_a_disabled_action_is_not_offered_for_dragging(page):
    page.evaluate("() => { __winnow.S.pluginRowActions = [{ id: 'demo.vt', local_id: 'vt', plugin: 'demo', "
                  "plugin_fs: 'demo', label: 'Look up on VT', description: 'demo', max_rows: 2 }]; }")
    try:
        page.locator(".row").nth(0).locator(".cell").nth(1).click()
        page.locator(".row").nth(3).locator(".cell").nth(1).click(modifiers=["Shift"])   # 4 rows > max 2
        _open(page, row=1)
        sub = _sub(page, "Plugins")
        item = sub.locator(".menu-pinnable .menu-item", has_text="Look up on VT")
        assert item.is_disabled() and item.get_attribute("draggable") is None
        assert sub.locator(".menu-pinnable .menu-pin-btn").count() == 1   # the star still works
        assert sub.locator(".menu-item-note").inner_text() == "demo"       # a note, not a keycap
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
    finally:
        page.evaluate("() => { __winnow.S.pluginRowActions = []; }")


def test_arrow_keys_walk_into_and_out_of_a_flyout(page):
    _open(page)
    page.locator(".menu:not(.menu-sub) .menu-item").first.focus()
    page.keyboard.press("ArrowRight")                 # the first item is Tag this row ▸
    page.wait_for_selector(".menu-sub")
    assert page.evaluate("() => document.activeElement.closest('.menu-sub') !== null")
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => document.activeElement.closest('.menu-sub') !== null")
    page.keyboard.press("ArrowLeft")
    page.wait_for_selector(".menu-sub", state="detached")
    assert page.evaluate("() => document.activeElement.classList.contains('menu-item-sub')")
    assert page.evaluate("() => document.activeElement.getAttribute('aria-expanded')") == "false"
    page.keyboard.press("Escape")
