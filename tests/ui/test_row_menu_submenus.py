"""The row menu, one level deep at the top. The clicked column's filters
stay broken out; Tag, Add to dashboard, Copy and Plugins fold into
submenus that open on click (and on hover). Items in a submenu that
declare a pin id can be starred or dragged onto the top of the menu,
where they stay across reopens, per machine — and vanish quietly while
their plugin is off."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui


# "Add to dashboard" only appears in the menus when the case has opted in
# (Case settings → Dashboards); the whole module assumes it has.
@pytest.fixture(autouse=True, scope="module")
def _dashboard_creator_mode(server):
    _post_setting(server, {"dashboard_creator": True})
    yield
    _post_setting(server, {"dashboard_creator": False})


def _post_setting(server, body):
    import json
    import urllib.request
    req = urllib.request.Request(
        server.rstrip("/") + "/api/case_settings", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    urllib.request.urlopen(req, timeout=10).read()


@pytest.fixture(autouse=True)
def _fresh_case_settings(page, _dashboard_creator_mode):
    """The page fixture is shared; make sure it has read the setting."""
    page.evaluate("() => __winnow.loadCaseSettings()")


def _root_text(page):
    return page.locator(".menu:not(.menu-sub)").inner_text()


def _clear_pins(page):
    page.evaluate("() => localStorage.removeItem('winnow.menupins')")


def _pin_tag(page, index=0):
    """Pin a tag by NAME (ids are per case file and get reused) and return the name."""
    name = page.evaluate("(i) => __winnow.S.tags[i].name", index)
    page.evaluate("(n) => { localStorage.setItem('winnow.menupins', JSON.stringify({ row: ['tag:' + n] })); }", name)
    return name


def _kinds(page):
    """The root menu's children as a shape: '-' for a rule, 'H' for a header,
    else the first word of the item."""
    kinds = page.evaluate("""() => [...document.querySelector('.menu:not(.menu-sub)').children].map(n =>
        n.classList.contains('menu-sep') ? '-' : n.classList.contains('menu-header') ? 'H' :
        n.classList.contains('menu-dropzone') ? '' : (n.querySelector('.menu-item-text') || n).textContent.trim().split(' ')[0])""")
    return [k for k in kinds if k]


def test_top_level_is_short_and_keeps_the_filters_broken_out(page, row_menu):
    root = row_menu(row=1, cell=1)
    labels = [t.replace("\n", " ") for t in root.locator(".menu-item").all_inner_texts()]
    subs = [t.replace("\n", " ") for t in root.locator(".menu-item-sub").all_inner_texts()]
    assert any(l.startswith("Tag this row") for l in subs)
    assert any(l.startswith("Add to dashboard") for l in subs)
    assert any(l.startswith("Copy") for l in subs)
    # the filters are plain items at the top, not folded
    assert any(l.startswith("Filter to") for l in labels) and any(l.startswith("Exclude") for l in labels)
    assert len(labels) <= 8, labels
    page.keyboard.press("Escape")


def test_tag_submenu_toggles_in_place_and_keeps_its_hotkeys(page, row_menu, flyout):
    row_menu(row=3, cell=1)
    sub = flyout("Tag this row")
    first = sub.locator(".menu-item").first
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


def test_the_tag_entry_advertises_the_hotkeys_the_tags_carry(page, row_menu):
    keys = sorted(k for k in page.evaluate("() => __winnow.S.tags.map((t) => t.hotkey)") if k)
    root = row_menu(row=1, cell=1)
    hint = root.locator(".menu-item-sub", has_text="Tag this row").locator(".menu-item-hint").inner_text()
    assert hint == (f"{keys[0]}–{keys[-1]}" if len(keys) > 1 else (keys[0] if keys else ""))
    page.keyboard.press("Escape")


def test_hovering_a_sibling_closes_the_flyout(page, row_menu, flyout):
    row_menu(row=1, cell=1)
    flyout("Copy")
    page.locator(".menu:not(.menu-sub) .menu-item", has_text="Filter to").first.hover()
    page.wait_for_selector(".menu-sub", state="detached")
    page.keyboard.press("Escape")


def test_starring_a_plugin_action_pins_it_and_it_survives_a_reopen(page, row_menu, flyout, fake_row_action):
    fake_row_action()
    _clear_pins(page)
    try:
        row_menu(row=1, cell=1)
        assert "Pinned" not in _root_text(page)
        sub = flyout("Plugins")
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

        # persisted: the store is re-read from localStorage on every open
        assert "plugin:demo:vt" in page.evaluate("() => localStorage.getItem('winnow.menupins')")
        row_menu(row=4, cell=1)
        assert page.locator(".menu:not(.menu-sub) .menu-pinnable", has_text="Look up on VT").count() == 1
        # ✕ unpins
        page.locator(".menu:not(.menu-sub) .menu-pinnable", has_text="Look up on VT").locator(".menu-pin-btn").click()
        page.wait_for_function("() => !document.querySelector('.menu:not(.menu-sub) .menu-pinnable')")
        assert "Look up on VT" not in _root_text(page)
        page.keyboard.press("Escape")
    finally:
        _clear_pins(page)


def test_dragging_out_of_a_submenu_pins_it(page, row_menu, flyout, fake_row_action):
    fake_row_action()
    _clear_pins(page)
    try:
        row_menu(row=1, cell=1)
        sub = flyout("Plugins")
        item = sub.locator(".menu-pinnable .menu-item", has_text="Look up on VT")
        assert item.get_attribute("draggable") == "true"
        item.drag_to(page.locator(".menu:not(.menu-sub) .menu-item-sub", has_text="Copy"))
        page.wait_for_selector(".menu:not(.menu-sub) .menu-header:has-text('Pinned')")
        assert page.locator(".menu:not(.menu-sub) .menu-pinnable", has_text="Look up on VT").count() == 1
        page.keyboard.press("Escape")
    finally:
        _clear_pins(page)


def test_a_pinned_action_runs_and_hides_while_its_plugin_is_off(page, row_menu, fake_row_action):
    fake_row_action()
    page.evaluate("() => { localStorage.setItem('winnow.menupins', JSON.stringify({ row: ['plugin:demo:vt', 'copy:cell'] })); }")
    try:
        top = row_menu(row=1, cell=1)
        assert top.locator(".menu-pinnable", has_text="Look up on VT").count() == 1
        assert top.locator(".menu-pinnable", has_text="Copy cell").count() == 1
        page.keyboard.press("Escape")
        # plugin off: its pin is not shown, the other pin still is
        page.evaluate("() => { __winnow.S.pluginRowActions = []; }")
        top = row_menu(row=1, cell=1)
        assert top.locator(".menu-pinnable", has_text="Look up on VT").count() == 0
        assert top.locator(".menu-pinnable", has_text="Copy cell").count() == 1
        # the pin survives for when the plugin returns
        assert "plugin:demo:vt" in page.evaluate("() => localStorage.getItem('winnow.menupins')")
        # a pinned copy action still does its job
        page.locator(".menu:not(.menu-sub) .menu-pinnable .menu-item", has_text="Copy cell").click()
        page.wait_for_selector(".menu", state="detached")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:   # polled from Python — see tests/test_ui_test_hygiene.py
            if page.evaluate("() => navigator.clipboard.readText().then((t) => t.length > 0)"):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("nothing reached the clipboard")
    finally:
        _clear_pins(page)


def test_toggling_a_plugin_off_takes_its_row_actions_with_it(page, api):
    """The plugins panel copies row_actions from the toggle response, so a
    pinned action cannot outlive its plugin within a session (a stale list
    would show the pin and 404 on click)."""
    plugins = api("/api/plugins")["plugins"]
    p = next((p for p in plugins if p.get("bundled") and p.get("case_override") is None), None)
    if p is None:
        pytest.skip("no bundled plugin to toggle")
    flip, restore = ("off_all", "on_all") if p.get("machine_enabled") else ("on_all", "off_all")
    # A stand-in the server never listed: the toggle's refresh must drop it.
    page.evaluate("() => __winnow.S.pluginRowActions.push({ id: 'ghost', local_id: 'ghost', plugin: 'ghost', "
                  "plugin_fs: 'ghost', label: 'Ghost action', description: '', max_rows: 5 })")
    page.keyboard.press("?")
    page.wait_for_selector("#modal:not([hidden])")
    page.click(".settings-section-head:has-text('Plugins')")
    # The panel lists plugins after its own fetch, one scope select per
    # entry of S.plugins in order; a plugin's display name changes once it
    # loads, so it is found by position, keyed on fs_name.
    find = ("(fs) => { const i = __winnow.S.plugins.findIndex((q) => q.fs_name === fs);"
            " const sels = [...document.querySelectorAll('#modalBody select')]"
            ".filter((s) => s.nextElementSibling && s.nextElementSibling.classList.contains('session-name'));"
            " const sel = sels[i]; return sel && !sel.disabled ? sel : null; }")
    set_scope = "([fs, value]) => { const sel = (%s)(fs); sel.value = value; sel.dispatchEvent(new Event('change')); }" % find
    page.wait_for_function("(fs) => !!(%s)(fs)" % find, arg=p["fs_name"], timeout=10_000)
    page.evaluate(set_scope, [p["fs_name"], flip])
    try:
        page.wait_for_function("() => !__winnow.S.pluginRowActions.some((a) => a.id === 'ghost')", timeout=10_000)
    finally:
        page.wait_for_function("(fs) => !!(%s)(fs)" % find, arg=p["fs_name"], timeout=10_000)
        page.evaluate(set_scope, [p["fs_name"], restore])
        page.wait_for_function("([fs, on]) => (__winnow.S.plugins.find((q) => q.fs_name === fs) || {}).machine_enabled === on",
                               arg=[p["fs_name"], restore == "on_all"], timeout=10_000)
        page.keyboard.press("Escape")


def test_pinned_tag_toggles_from_the_top_level(page, row_menu):
    _pin_tag(page)
    try:
        row_menu(row=5, cell=1)
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


def test_hover_then_click_leaves_the_flyout_open(page, row_menu):
    row_menu(row=1, cell=1)
    parent = page.locator(".menu:not(.menu-sub) .menu-item-sub", has_text="Copy")
    parent.hover()
    page.wait_for_selector(".menu-sub")          # hover opened it
    parent.click()                                # the click must not toggle it shut
    page.wait_for_timeout(250)                    # longer than any close timer the hover armed
    assert page.locator(".menu-sub").count() == 1
    assert parent.get_attribute("aria-expanded") == "true"
    page.keyboard.press("Escape")


def test_rules_sit_before_and_after_the_filter_block_only(page, row_menu):
    row_menu(row=1, cell=1)
    kinds = _kinds(page)
    seps = [i for i, k in enumerate(kinds) if k == "-"]
    assert len(seps) == 2, kinds
    assert kinds[seps[0] + 1] == "H"                  # the column header opens the block
    assert kinds[seps[1] - 1].startswith("Filter")    # the last filter item closes it
    assert kinds[seps[1] + 1] == "Add"                # then the folded entries
    page.keyboard.press("Escape")


def test_a_gutter_click_still_rules_undo_off_from_the_folds(page, flyout):
    """No column under the pointer: no filter block, so the folds run
    together — until Undo appears, which gets a rule after it."""
    page.locator(".row").nth(8).locator(".gutter").click(button="right")
    page.wait_for_selector(".menu:not(.menu-sub)")
    assert "H" not in _kinds(page)
    dash = page.locator(".menu:not(.menu-sub) .menu-item-sub", has_text="Add to dashboard")
    assert dash.count() == 1                          # offered, its one item greyed
    flyout("Add to dashboard")
    assert page.locator(".menu-sub .menu-item").first.is_disabled()
    sub = flyout("Tag this row")
    sub.locator(".menu-item").first.click()
    page.wait_for_function("() => (__winnow.rowAt(8) || { tags: [] }).tags.length === 1")
    page.wait_for_selector(".menu:not(.menu-sub) .menu-item:has-text('Undo')")
    kinds = _kinds(page)
    undo = next(i for i, k in enumerate(kinds) if k.startswith("Undo"))
    assert kinds[undo + 1] == "-", kinds
    assert kinds.count("-") == 1, kinds
    page.locator(".menu:not(.menu-sub) .menu-item", has_text="Undo").click()
    page.wait_for_function("() => (__winnow.rowAt(8) || { tags: [] }).tags.length === 0")


def test_undo_appears_at_the_top_after_a_pinned_tag(page, row_menu):
    """Holds on a fresh server too: the repaint waits for the undo state
    the tagging changed, rather than reading a stale one."""
    _pin_tag(page)
    try:
        row_menu(row=6, cell=1)
        page.locator(".menu:not(.menu-sub) .menu-pinnable .menu-item").first.click()
        page.wait_for_function("() => (__winnow.rowAt(6) || { tags: [] }).tags.length === 1")
        page.wait_for_selector(".menu:not(.menu-sub) .menu-item:has-text('Undo')")
        page.locator(".menu:not(.menu-sub) .menu-item", has_text="Undo").click()
        page.wait_for_function("() => (__winnow.rowAt(6) || { tags: [] }).tags.length === 0")
    finally:
        _clear_pins(page)


def test_pinned_tag_and_its_flyout_twin_agree(page, row_menu, flyout):
    _pin_tag(page)
    try:
        row_menu(row=7, cell=1)
        sub = flyout("Tag this row")
        sub.locator(".menu-item").first.click()          # toggle inside the flyout
        page.wait_for_function("() => (__winnow.rowAt(7) || { tags: [] }).tags.length === 1")
        # the flyout stayed open and the pinned twin at the top caught up
        page.wait_for_selector(".menu:not(.menu-sub) .menu-pinnable .menu-check:has-text('✓')")
        assert page.locator(".menu-sub .menu-item").first.locator(".menu-check").inner_text() == "✓"
        # and clicking the pinned twin now REMOVES rather than re-applying
        page.locator(".menu:not(.menu-sub) .menu-pinnable .menu-item").first.click()
        page.wait_for_function("() => (__winnow.rowAt(7) || { tags: [] }).tags.length === 0")
        page.keyboard.press("Escape")
    finally:
        _clear_pins(page)


def test_an_open_flyout_follows_the_repainted_root(page, row_menu, flyout):
    """A keepOpen click rebuilds the root under an open flyout. The flyout
    must re-bind to its parent's NEW button — aria-expanded on the live
    node, the flyout moved with it when Undo pushed it down — and a
    sibling that inherited the old position must still open its own."""
    row_menu(row=9, cell=1)
    sub = flyout("Tag this row")
    sub.locator(".menu-item").first.click()          # keepOpen → repaint; Undo appears above Copy
    page.wait_for_function("() => (__winnow.rowAt(9) || { tags: [] }).tags.length === 1")
    page.wait_for_selector(".menu:not(.menu-sub) .menu-item:has-text('Undo')")
    parent = page.locator(".menu:not(.menu-sub) .menu-item-sub", has_text="Tag this row")
    assert parent.get_attribute("aria-expanded") == "true"
    top = page.evaluate("() => [document.querySelector('.menu-sub').getBoundingClientRect().top,"
                        " document.querySelector('.menu:not(.menu-sub) .menu-item-sub').getBoundingClientRect().top]")
    assert abs(top[0] - top[1]) < 12, top
    # Left from the flyout lands on the live parent, not a detached one
    page.locator(".menu-sub .menu-item").first.focus()
    page.keyboard.press("ArrowLeft")
    page.wait_for_selector(".menu-sub", state="detached")
    assert page.evaluate("() => document.activeElement.isConnected && document.activeElement.textContent.startsWith('Tag')")
    # the sibling one row down opens ITS flyout, not the tag list
    flyout("Add to dashboard")
    assert "Count of" in page.locator(".menu-sub").inner_text()
    page.locator(".menu:not(.menu-sub) .menu-item", has_text="Undo").click()
    page.wait_for_function("() => (__winnow.rowAt(9) || { tags: [] }).tags.length === 0")


def test_a_tag_hotkey_pressed_with_the_flyout_open_repaints_it(page, row_menu, flyout):
    row_menu(row=10, cell=1)
    sub = flyout("Tag this row")
    key = sub.locator(".menu-item").first.locator(".menu-item-hint").inner_text()
    page.keyboard.press(key)
    page.wait_for_function("() => (__winnow.rowAt(10) || { tags: [] }).tags.length === 1")
    page.wait_for_selector(".menu-sub .menu-item .menu-check:has-text('✓')")
    page.keyboard.press(key)                          # and off again, ✓ gone
    page.wait_for_function("() => (__winnow.rowAt(10) || { tags: [] }).tags.length === 0")
    page.wait_for_function("() => document.querySelector('.menu-sub .menu-item .menu-check').textContent === ''")
    page.keyboard.press("Escape")


def test_a_disabled_action_is_not_offered_for_dragging(page, row_menu, flyout, fake_row_action):
    fake_row_action(max_rows=2)
    page.locator(".row").nth(0).locator(".cell").nth(1).click()
    page.locator(".row").nth(3).locator(".cell").nth(1).click(modifiers=["Shift"])   # 4 rows > max 2
    row_menu(row=1, cell=1)
    sub = flyout("Plugins")
    item = sub.locator(".menu-pinnable .menu-item", has_text="Look up on VT")
    assert item.is_disabled() and item.get_attribute("draggable") is None
    assert sub.locator(".menu-pinnable .menu-pin-btn").count() == 1   # the star still works
    assert sub.locator(".menu-item-note").inner_text() == "demo"       # a note, not a keycap
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")


def test_arrow_keys_walk_into_and_out_of_a_flyout(page, row_menu):
    row_menu(row=1, cell=1)
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


def test_arrows_from_outside_the_menu_step_in_at_either_end(page, row_menu):
    row_menu(row=1, cell=1)                          # a right-click leaves focus on the body
    page.keyboard.press("ArrowUp")
    assert page.evaluate("() => { const all = [...document.querySelectorAll('.menu:not(.menu-sub) .menu-item:not(:disabled)')];"
                         " return all[all.length - 1] === document.activeElement; }")
    page.keyboard.press("Escape")
    row_menu(row=1, cell=1)
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => document.querySelector('.menu:not(.menu-sub) .menu-item:not(:disabled)') === document.activeElement")
    page.keyboard.press("Escape")


def test_a_menu_leaves_a_text_fields_arrows_alone(page, row_menu):
    box = page.locator("#sidebarFilter")
    box.click()
    box.fill("abc")
    box.press("Home")
    row_menu(row=1, cell=1)
    page.evaluate("() => document.getElementById('sidebarFilter').focus()")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    assert page.evaluate("() => document.activeElement.selectionStart") == 2
    page.keyboard.press("ArrowDown")                  # not stolen either: the field keeps focus
    assert page.evaluate("() => /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)")
    page.keyboard.press("Escape")
    box.fill("")
