"""Right-clicking a table — its tab, or its row in the sidebar — opens a
short menu at the pointer, not the Table dialog.

It used to open the full-height Table dialog (every column, the value
dropdowns, layout defaults) for what was most often a close or a rename.
The dialog is one entry in the menu now, **Columns…**, and `C` still opens
it on its own. Every other entry is the call the app already made for the
same thing elsewhere — Close this tab and the nickname from the dialog,
the merge builder, the Tables manager's Remove… with its confirm — so
these tests check that each entry lands in that existing flow, and that
the menu says what the existing flows refuse (no Close on a closed table,
no Remove for a table a merge is built on).
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui

HEADER = "Timestamp,EventId,Host,ExtremelyLongColumnHeaderName,CommandLine"
EXTRA = "ctxmenu.csv"
ROOT = ".menu:not(.menu-sub)"


def _labels(page):
    return page.locator(f"{ROOT} .menu-item .menu-item-text").all_inner_texts()


def _item(page, label):
    return page.locator(f"{ROOT} .menu-item", has=page.locator(".menu-item-text", has_text=label))


def _tab(page, name):
    return page.locator("#sourceTabs .tab", has_text=name).first


def _tree_row(page, name):
    return page.locator("#sidebarList .sidebar-row:not(.sidebar-openrow)", has_text=name).first


def _source(page, name):
    return page.evaluate("(n) => __winnow.S.sources.find((s) => s.name === n) || null", name)


@pytest.fixture
def extra(page, tmp_path):
    """A second table of the fixture's shape, so closing or removing one
    never takes away the table every other module expects to find. Its
    merges and the table itself go again afterwards — the UI suite shares
    one case file."""
    f = tmp_path / EXTRA
    f.write_text(HEADER + "\n2026-03-15 09:00:00,4624,H9,v,cmd.exe\n", encoding="utf-8")
    page.evaluate(
        """([path, name]) => __winnow.post('/api/ingest/jobs/path',
             { path, name, kind: 'csv' }).then(() => __winnow.startJobsPoll())""",
        [str(f), EXTRA])
    # Polled from Python: wait_for_function does not await a promise.
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        names = page.evaluate(
            "() => __winnow.loadSources(undefined, { navigate: false }).then(() => __winnow.S.sources.map((s) => s.name))")
        if EXTRA in names:
            break
        time.sleep(0.25)
    else:
        pytest.fail(f"{EXTRA} never appeared")
    page.evaluate("""(n) => (async () => {
        const s = __winnow.S.sources.find((x) => x.name === n);
        await __winnow.post('/api/source/' + s.id + '/open', { open: true });
        await __winnow.loadSources(undefined, { navigate: false });
      })()""", EXTRA)
    _tab(page, EXTRA).wait_for()
    yield _source(page, EXTRA)
    page.evaluate("""(n) => (async () => {
        const s = __winnow.S.sources.find((x) => x.name === n);
        for (const m of __winnow.S.sources.filter((x) => x.is_merge && s && x.member_source_ids.includes(s.id))) {
          await __winnow.api('/api/merges/' + (-m.id), { method: 'DELETE' });
        }
        if (s) await __winnow.api('/api/source/' + s.id, { method: 'DELETE' });
        __winnow.S.viewCache.clear();
        __winnow.S.sourceId = null;
        await __winnow.loadSources();
      })()""", EXTRA)
    page.wait_for_selector(".row")


def test_right_click_on_a_tab_opens_a_short_menu_at_the_pointer(page):
    tab = page.locator("#sourceTabs .tab").first
    box = tab.bounding_box()
    tab.click(button="right", position={"x": 24, "y": 8})
    page.wait_for_selector(ROOT)
    # The symptom: a right-click put up the full-height Table dialog.
    assert page.locator("#modal").is_hidden(), "the right-click opened the Table dialog again"
    assert _labels(page) == [
        "Close this tab", "Nickname this table…", "Columns…", "Merge with other tables…", "Remove…",
    ], _labels(page)
    menu = page.locator(ROOT).bounding_box()
    # At the pointer, like the row and column-header menus.
    assert abs(menu["x"] - (box["x"] + 24)) <= 2, (menu, box)
    assert abs(menu["y"] - (box["y"] + 8 + 4)) <= 2, (menu, box)
    page.keyboard.press("Escape")
    page.wait_for_selector(ROOT, state="detached")


def test_columns_opens_the_table_dialog_and_c_still_toggles_it(page):
    page.locator("#sourceTabs .tab").first.click(button="right")
    page.wait_for_selector(ROOT)
    # The table on screen carries the dialog's own key beside the entry.
    assert _item(page, "Columns…").locator(".menu-item-hint").inner_text() == "C"
    _item(page, "Columns…").click()
    page.wait_for_selector("#modal:not([hidden]) .table-menu")
    assert page.locator("#modalTitle").inner_text().upper().startswith("TABLE —")
    assert page.locator(".table-menu .side-rail-item").count() >= 3
    strip = page.locator("#modalBody .table-menu-strip button").all_inner_texts()
    assert strip == ["Tables manager…", "Reset view", "Close this tab"], strip
    # Still the openTableMenu dialog, so its key closes it however it was opened.
    page.keyboard.press("C")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_the_context_menu_key_opens_it_under_the_focused_tab(page):
    """Keyboard opening: Chromium fires the same contextmenu event at the
    tab's centre, which would drop the menu over the tab it is about."""
    tab = page.locator("#sourceTabs .tab").first
    tab.focus()
    page.keyboard.press("ContextMenu")
    page.wait_for_selector(ROOT)
    box = tab.bounding_box()
    menu = page.locator(ROOT).bounding_box()
    assert menu["y"] >= box["y"] + box["height"], (menu, box)
    assert abs(menu["x"] - box["x"]) <= 2, (menu, box)
    # Down steps in, the way every menu here is walked from the keyboard.
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => document.activeElement.textContent") == "Close this tab"
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => document.activeElement.querySelector('.menu-item-text').textContent") == "Columns…"
    page.keyboard.press("Enter")
    page.wait_for_selector("#modal:not([hidden]) .table-menu")
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_close_matches_the_tab_x_and_a_closed_table_has_no_close(page, extra):
    _tab(page, EXTRA).click(button="right")
    page.wait_for_selector(ROOT)
    _item(page, "Close this tab").click()
    page.wait_for_function(
        "(n) => ![...document.querySelectorAll('#sourceTabs .tab')].some((t) => t.textContent.includes(n))",
        arg=EXTRA)
    # Closed, not removed: still in the case, and still in the tree.
    assert _source(page, EXTRA)["is_open"] is False
    _tree_row(page, EXTRA).click(button="right")
    page.wait_for_selector(ROOT)
    assert _labels(page) == ["Nickname this table…", "Columns…", "Merge with other tables…", "Remove…"], _labels(page)
    # Not the table on screen, so no keycap promising `C` opens this one.
    assert _item(page, "Columns…").locator(".menu-item-hint").count() == 0
    # Columns… on a closed table opens it first, then the dialog on it.
    _item(page, "Columns…").click()
    page.wait_for_selector("#modal:not([hidden]) .table-menu")
    assert EXTRA.upper() in page.locator("#modalTitle").inner_text().upper()
    assert page.evaluate("(id) => __winnow.S.sourceId === id", extra["id"])
    page.keyboard.press("Escape")


def test_nickname_goes_through_the_existing_prompt(page, extra):
    _tab(page, EXTRA).click(button="right")
    page.wait_for_selector(ROOT)
    _item(page, "Nickname this table…").click()
    dlg = page.locator(".confirm-overlay")
    dlg.wait_for()
    assert f'Nickname for "{EXTRA}"' in dlg.inner_text()
    page.locator(".confirm-overlay input").fill("Ctx nick")
    page.locator(".confirm-card .btn", has_text="Save").click()
    page.wait_for_function(
        "() => [...document.querySelectorAll('#sourceTabs .tab')].some((t) => t.textContent.includes('Ctx nick'))")


def test_merge_starts_the_builder_with_this_table_ticked(page, extra):
    _tab(page, EXTRA).click(button="right")
    page.wait_for_selector(ROOT)
    _item(page, "Merge with other tables…").click()
    page.wait_for_selector("#modal .mb")
    box = page.locator("#modal label.mb-trow", has_text=EXTRA).locator("input[type=checkbox]")
    assert box.is_checked()
    page.keyboard.press("Escape")


def test_remove_asks_first_and_then_removes(page, extra):
    openrow = page.locator("#sidebarList .sidebar-openrow", has_text=EXTRA).first
    openrow.click(button="right")
    page.wait_for_selector(ROOT)
    _item(page, "Remove…").click()
    dlg = page.locator(".confirm-overlay")
    dlg.wait_for()
    assert f"Remove {EXTRA} from this case?" in dlg.inner_text()
    page.keyboard.press("Escape")
    page.wait_for_selector(".confirm-overlay", state="detached")
    assert _source(page, EXTRA) is not None, "cancelling the confirm removed the table anyway"

    openrow.click(button="right")
    page.wait_for_selector(ROOT)
    _item(page, "Remove…").click()
    page.locator(".confirm-overlay .btn.danger", has_text="Remove").click()
    page.wait_for_function("(n) => !__winnow.S.sources.some((s) => s.name === n)", arg=EXTRA)
    # Waited for, not looked at once: loadSources() swaps S.sources before
    # it repaints the strip, with a fetch in between.
    _tab(page, EXTRA).wait_for(state="detached")


def test_a_merged_table_cannot_be_removed_and_a_merge_offers_its_own_verbs(page, extra):
    ui = _source(page, "ui.csv")
    page.evaluate("""async (ids) => {
        await __winnow.post('/api/merges', { name: 'Ctx merge', source_ids: ids });
        await __winnow.loadSources(undefined, { navigate: false });
      }""", [ui["id"], extra["id"]])
    _tree_row(page, EXTRA).click(button="right")
    page.wait_for_selector(ROOT)
    remove = _item(page, "Remove…")
    # Greyed out with the merge named on the item, rather than an entry
    # that looks live and then refuses.
    assert remove.is_disabled()
    assert '"Ctx merge"' in remove.locator(".menu-item-desc").inner_text()
    assert "Delete that merge first" in remove.get_attribute("title")
    page.keyboard.press("Escape")

    _tree_row(page, "Ctx merge").click(button="right")
    page.wait_for_selector(ROOT)
    labels = [x for x in _labels(page) if x != "Close this tab"]
    assert labels == ["Rename this merge…", "Columns…", "Edit merge…", "Remove…"], labels
    assert not _item(page, "Remove…").is_disabled()
    _item(page, "Edit merge…").click()
    page.wait_for_selector("#modal .mb")
    for name in ("ui.csv", EXTRA):
        assert page.locator("#modal label.mb-trow", has_text=name).locator("input").is_checked(), name
    assert "Save changes" in page.locator("#modal button.mb-create").text_content()
    page.keyboard.press("Escape")
