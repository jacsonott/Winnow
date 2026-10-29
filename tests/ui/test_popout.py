"""Page tabs in windows of their own (static/js/popout.js).

What the analyst asked for was the Pivot beside the table it counts, rather
than taking turns with it. These pin what that takes: the pop-out shows the
page and none of the main window's chrome; the main window stops showing
the page (two live copies would both save the same tab state, last write
wins) and gets it back when the pop-out is docked or closed; what one
window writes the other repaints; navigation from a pop-out lands in the
main window, where the table is; and the pop-out goes with its main window
rather than outliving it.

The Timeline is the page under test for most of them: it is built in, needs
no plugin switched on, and repaints from tags, which is the cross-window
case that matters most.
"""

from __future__ import annotations

import contextlib

import pytest
from playwright.sync_api import Error as PlaywrightError

from spawn import running_server

pytestmark = pytest.mark.ui

# What a pop-out shows once it has booted onto its page.
POPPED = "document.documentElement.hasAttribute('data-popout') && !document.getElementById('popoutBar').hidden"


@contextlib.contextmanager
def popped_out(page, key="timeline", tab="#tabTimeline", open_menu=None):
    """Pop a page out from its tab's menu (or whatever `open_menu` right-
    clicks to get that menu). Yields the pop-out, checked for uncaught
    errors like `page` is, and closed on the way out if the test didn't —
    then waits for the main window to have the page back, so the next test
    starts from a main window with nothing out."""
    if open_menu:
        open_menu()
    else:
        page.click(tab, button="right")
    with page.expect_popup() as info:
        page.locator(".menu:not(.menu-sub) .menu-item", has_text="Open in its own window").click()
    pop = info.value
    errors: list[str] = []
    pop.on("pageerror", lambda e: errors.append(str(e)))
    pop.wait_for_load_state()
    pop.wait_for_function(POPPED)
    try:
        yield pop
    finally:
        if not pop.is_closed():
            pop.close()
        if not page.is_closed():
            page.wait_for_function("(k) => !window.__winnow.isPoppedOut(k)", arg=key)
    assert not errors, "uncaught JS errors in the pop-out: " + " | ".join(errors)


def _dock(pop):
    """Click ⇤ Back to main window, which closes the window it is in. The
    window can be gone before Playwright hears the click land, and it says
    so as an error; that is the click having worked. Whether the window
    really closed is what expect_event checks."""
    with pop.expect_event("close"):
        try:
            pop.click("#btnPopoutDock")
        except PlaywrightError as e:
            if "closed" not in str(e):
                raise


def _show_placeholder(page):
    page.click("#tabTimeline")
    page.wait_for_selector("#popoutPlaceholder:not([hidden])")


def test_pop_out_shows_the_page_alone_and_main_stands_down(page):
    page.click("#tabTimeline")
    page.wait_for_selector("#timelineview:not([hidden])")
    with popped_out(page) as pop:
        pop.wait_for_selector("#timelineview:not([hidden])")
        # The page, and none of the main window's chrome.
        for sel in ("#sidebar", "header.bar", "#toolbar"):
            assert not pop.locator(sel).is_visible(), sel
        assert pop.locator("#popoutPage").inner_text() == "Timeline"
        # The main window went back to the table it had open, and marks the tab.
        page.wait_for_selector("#grid .row")
        assert page.locator("#tabTimeline").evaluate("(b) => b.classList.contains('tab-popped')")
        # Asking for the page there shows where it is instead of a second copy.
        _show_placeholder(page)
        assert page.locator("#timelineview").is_hidden()
        assert "own window" in page.locator("#popoutPlaceholder").inner_text()


def test_back_to_main_window_closes_the_pop_out_and_shows_the_page(page):
    with popped_out(page) as pop:
        _show_placeholder(page)
        _dock(pop)
        page.wait_for_selector("#timelineview:not([hidden])")
        assert page.locator("#popoutPlaceholder").is_hidden()
        assert not page.locator("#tabTimeline").evaluate("(b) => b.classList.contains('tab-popped')")


def test_closing_the_pop_out_gives_the_page_back(page):
    """The window's own ✕ is how most pop-outs will end. Nothing in the page
    is clicked, so the main window learns of it from the pop-out's last
    word, or failing that from the window handle."""
    with popped_out(page) as pop:
        _show_placeholder(page)
        pop.close()
        page.wait_for_selector("#timelineview:not([hidden])")
        assert not page.evaluate("() => __winnow.isPoppedOut('timeline')")


@pytest.fixture
def pages_dropdown(page):
    """Settings → Pages as a dropdown, for this test only."""
    def _set(on):
        page.evaluate("""(on) => { __winnow.S.appearance.pagesMenu = on;
          __winnow.renderPageTabs(); __winnow.applyPageTabsSize(); }""", on)
    _set(True)
    page.wait_for_selector("#pagesMenuBtn")
    try:
        yield
    finally:
        _set(False)


def _right_click_in_pages_list(page, label):
    page.click("#pagesMenuBtn")
    page.locator(".menu:not(.menu-sub) .menu-item", has_text=label).click(button="right")
    page.wait_for_selector(".menu:not(.menu-sub) .menu-item:has-text('Close this tab')")


def test_with_pages_as_a_dropdown_a_page_pops_out_from_its_row(page, pages_dropdown):
    """With the tabs collapsed into Pages ▾ there was no tab left to
    right-click, so "Open in its own window" was nowhere but the
    sidebar's ⧉. The page's row in the list carries the tab's menu."""
    with popped_out(page, open_menu=lambda: _right_click_in_pages_list(page, "Timeline")) as pop:
        pop.wait_for_selector("#timelineview:not([hidden])")
        page.wait_for_function("() => __winnow.isPoppedOut('timeline')")
        # The row says where the page is, as its tab would, and its menu
        # offers the window rather than a second one.
        page.click("#pagesMenuBtn")
        row = page.locator(".menu:not(.menu-sub) .menu-item", has_text="Timeline")
        assert row.locator(".menu-item-note").inner_text() == "\u29c9"
        row.click(button="right")
        page.wait_for_selector(".menu:not(.menu-sub) .menu-item:has-text('Bring it back as a tab')")
        assert page.locator(".menu .menu-item", has_text="Open in its own window").count() == 0
        page.keyboard.press("Escape")


def test_with_pages_as_a_dropdown_the_button_has_the_showing_pages_menu(page, pages_dropdown):
    """The button carries the name of the page that is up, so a right-click
    on it means that page. With no page up it names none, and opens the
    list instead."""
    page.evaluate("() => __winnow.showGridTab()")
    page.wait_for_function("() => document.getElementById('pagesMenuBtn').textContent.includes('Pages')")
    page.click("#pagesMenuBtn", button="right")
    page.wait_for_selector(".menu:not(.menu-sub) .menu-item:has-text('Timeline')")
    assert page.locator(".menu .menu-item", has_text="Open in its own window").count() == 0
    page.keyboard.press("Escape")

    page.evaluate("() => __winnow.showTimelineTab()")
    page.wait_for_function("() => document.getElementById('pagesMenuBtn').textContent.includes('Timeline')")
    with popped_out(page, open_menu=lambda: page.click("#pagesMenuBtn", button="right")) as pop:
        pop.wait_for_selector("#timelineview:not([hidden])")


def test_a_tag_written_in_either_window_shows_in_the_other(page, api, tag_written):
    rows = page.locator("#body .row")
    pair = page.evaluate("() => [__winnow.rowAt(2).source_id, __winnow.rowAt(2).rid]")
    tag_id = page.evaluate("() => __winnow.S.tags.find((t) => String(t.hotkey) === '1').id")
    try:
        with popped_out(page) as pop:
            pop.wait_for_function("() => (__winnow.S.timeline.view || {}).row_count === 0")
            # Main window → pop-out: a tag key in the grid, and the Timeline
            # beside it rebuilds with the row on it.
            rows.nth(2).locator(".cell").nth(1).click()
            with tag_written(page):
                page.keyboard.press("1")
            pop.wait_for_function("() => (__winnow.S.timeline.view || {}).row_count === 1")
            # Pop-out → main window: the tag taken off from there (the route
            # the Timeline's own pane uses), and the grid's row loses it.
            pop.evaluate("([pair, tag]) => __winnow.post('/api/row_tags', { pairs: [pair], tag_id: tag, on: false })",
                         [pair, tag_id])
            page.wait_for_function("() => (__winnow.rowAt(2) || { tags: [1] }).tags.length === 0")
    finally:
        api("/api/row_tags", "POST", {"pairs": [pair], "tag_id": tag_id, "on": False})


def test_opening_a_line_in_the_pop_out_opens_its_row_in_the_main_window(page, api):
    """The pop-out has no grid. "Open this row" from its Timeline is the
    whole navigation, done where the table is."""
    pair = page.evaluate("() => [__winnow.rowAt(4).source_id, __winnow.rowAt(4).rid]")
    tag_id = page.evaluate("() => __winnow.S.tags.find((t) => String(t.hotkey) === '1').id")
    api("/api/row_tags", "POST", {"pairs": [pair], "tag_id": tag_id, "on": True})
    try:
        page.click("#tabNotes")
        page.wait_for_selector("#notesview:not([hidden])")
        with popped_out(page) as pop:
            pop.wait_for_selector("#timelineRows .timeline-row")
            with page.expect_response(lambda r: "/api/row_position?" in r.url
                                      and f"&source_id={pair[0]}&rid={pair[1]}" in r.url):
                pop.locator("#timelineRows .timeline-row").first.dblclick()
            page.wait_for_function("(id) => __winnow.S.activeTab === 'grid' && __winnow.S.sourceId === id",
                                   arg=pair[0])
            # And the pop-out is still showing its page, not a grid.
            assert pop.locator("#timelineview").is_visible()
            assert pop.evaluate("() => __winnow.S.activeTab") == "timeline"
    finally:
        api("/api/row_tags", "POST", {"pairs": [pair], "tag_id": tag_id, "on": False})


def test_reloading_the_main_window_keeps_its_pop_outs(page):
    with popped_out(page) as pop:
        page.reload()
        page.wait_for_selector("#grid .row")
        # The pop-out answers the reloaded window's hello: the tab is marked
        # again, and showing it shows the placeholder, not a second Timeline.
        page.wait_for_function("() => __winnow.isPoppedOut('timeline')")
        _show_placeholder(page)
        # Past the grace a pop-out gives a main window that said it was
        # leaving. Nothing to wait FOR: the check is that it did not close.
        pop.wait_for_timeout(5000)
        assert not pop.is_closed()
        assert pop.locator("#timelineview").is_visible()


def test_closing_the_main_window_closes_its_pop_outs(own_page):
    with own_page() as page:
        with popped_out(page) as pop:
            with pop.expect_event("close", timeout=15_000):
                page.close(run_before_unload=True)


def test_shut_down_does_not_count_its_own_pop_outs(page, until):
    """"Shut down" warns when other windows are open, because their work
    stops too. A pop-out is this window's own page, not someone else's."""
    with popped_out(page):
        # The server counts the pop-out (its presence stream is open)...
        raw = "() => fetch(`/api/presence/others?page=${__winnow.PAGE_ID}`).then((r) => r.json()).then((d) => d.others)"
        until(page, lambda: page.evaluate(raw) == 1, "the server to see the pop-out")
        # ...and the warning doesn't.
        assert page.evaluate("() => __winnow.otherWindows()") == 0


# ------------------------------------------------------------ the Pivot

PIVOT_STATE = "tab:pivot-table.pivot"

DRAG_FIELD = """(args) => {
  const [srcSel, dstSel] = args;
  const src = document.querySelector(srcSel);
  const dst = document.querySelector(dstSel);
  const dt = new DataTransfer();
  src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
  dst.dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt, cancelable: true }));
  dst.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt, cancelable: true }));
  src.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
}"""


def _is_state_post(r):
    return "/api/plugin_state" in r.url and r.request.method == "POST"


@pytest.fixture
def pivot(page, server_post):
    server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "on_all"})
    server_post("/api/plugin_state", {"key": PIVOT_STATE, "payload": None})
    try:
        page.evaluate("() => __winnow.loadPlugins()")
        page.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('pivot'))", timeout=10_000)
        yield page.evaluate("() => __winnow.S.pluginTabs.find((t) => t.id.includes('pivot')).id")
    finally:
        server_post("/api/plugin_state", {"key": PIVOT_STATE, "payload": None})
        server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "off_all"})


def test_the_pivot_moves_between_windows_with_what_was_built_in_it(page, pivot):
    """The case this was asked for. One live copy: the main window's mount
    goes when the Pivot pops out, and a field dropped in the pop-out a
    moment before it is docked — still inside tabState's debounce — is
    what the main window's fresh mount reads back."""
    tab = ".tab-plugin:has-text('Pivot')"
    page.click(tab)
    page.wait_for_selector(".pluginview [data-field]", timeout=10_000)
    ui_csv = str(page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'ui.csv').id"))
    if page.eval_on_selector(".pluginview select", "(s) => s.value") != ui_csv:
        with page.expect_response(_is_state_post, timeout=15_000):
            page.select_option(".pluginview select", ui_csv)
    page.wait_for_selector(".pluginview [data-field='Host']", timeout=10_000)
    with page.expect_response(_is_state_post, timeout=15_000):
        page.evaluate(DRAG_FIELD, ["[data-field='Host']", "[data-zone='rows']"])

    with popped_out(page, key=f"plugin:{pivot}", tab=tab) as pop:
        pop.wait_for_selector(".pluginview [data-zone='rows'] [data-field='Host']", timeout=10_000)
        page.wait_for_selector("#grid .row")
        assert page.locator(".pluginview").count() == 0, "the main window kept its Pivot mounted"

        pop.evaluate(DRAG_FIELD, ["[data-field='EventId']", "[data-zone='rows']"])
        _dock(pop)

        page.click(tab)
        page.wait_for_selector(".pluginview [data-zone='rows'] [data-field='EventId']", timeout=10_000)
        assert page.locator(".pluginview [data-zone='rows'] [data-field='Host']").count() == 1


# ------------------------------------------------- a case switch, own server


@pytest.fixture
def own_server(tmp_path):
    csv_path = tmp_path / "a.csv"
    csv_path.write_text("When,Host,Cmd\n" + "".join(f"2020-01-01 08:{i:02d}:00,H{i},cmd{i}\n" for i in range(30)),
                        encoding="utf-8")
    with running_server(tmp_path, "--case", tmp_path / "caseA.db", "--open", csv_path, "--no-fts",
                        env={"WINNOW_IDLE_EXIT_S": "600", "WINNOW_NEVER_CONNECTED_EXIT_S": "600"},
                        cwd=tmp_path, ready="/api/sources") as srv:
        yield {"base": srv.base, "post": srv.post, "dir": tmp_path}


def test_a_pop_out_follows_its_main_window_to_another_case(own_page, own_server):
    """Opening another case in the main window used to leave every other
    window on the case it had, covered by "This window is out of date". A
    pop-out is a page of the main window, so it goes with it."""
    with own_page(base=own_server["base"]) as page:
        with popped_out(page) as pop:
            assert pop.locator("#popoutCase").inner_text() == "caseA"
            case_b = str(own_server["dir"] / "caseB.db-winnow")
            own_server["post"]("/api/cases", {"path": case_b, "name": "Empty B", "group": "", "notes": ""})
            page.evaluate("(p) => __winnow.openCase(p)", case_b)
            page.wait_for_function("() => __winnow.S.sources.length === 0")
            pop.wait_for_function(f"() => {POPPED} && document.getElementById('popoutCase').textContent === 'Empty B'")
            assert pop.locator("#timelineview").is_visible()
            assert pop.locator("#caseGone").is_hidden()
            # And it is still the main window's: the tab there is marked.
            page.wait_for_function("() => __winnow.isPoppedOut('timeline')")
