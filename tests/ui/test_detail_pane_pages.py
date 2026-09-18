"""The row detail pane goes with the grid.

`#detail` and `#detailResize` sit beside `.main-content`, not inside the
grid, so a page switch that only swapped the main views left the pane
standing next to the SQL editor, the notes page or a plugin tab, showing a
row of a grid that wasn't on screen — and with `d` and Escape gated to the
grid, its own Close button was the only way out. Now it closes when a page
takes over the main area, stays closed when the grid comes back, and closes
when the grid leaves the table it was showing — a switch to another table,
or the last tab closing — while a refresh of the table already open, the
idiom behind adding a column or closing some other tab, leaves it be.
"""
import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui

# Every wait in this module is bounded. A page.evaluate that returns an app
# promise has no timeout at all — the browser CI job once sat on one of these
# tests for the runner's six-hour limit — so the app calls below are fired
# and then waited for with wait_for_function, and the server calls go
# through urllib with a timeout.


def _delete_source(server, sid):
    req = urllib.request.Request(server.rstrip("/") + f"/api/source/{sid}", method="DELETE",
                                 headers={"X-Timeline-Lite-Client": "1"})
    return urllib.request.urlopen(req, timeout=30).status

NOTE_BINDING = ("() => [document.getElementById('noteInput').dataset.rid,"
                " document.getElementById('noteInput').dataset.sourceId]")


def _open_pane(page):
    # A click first, so the cursor sits on the row the pane shows — that is
    # what the `d` toggle keys off when the pane is reopened later.
    page.locator(".row").first.locator(".cell").first.click()
    page.evaluate("() => __winnow.showDetail(0)")
    page.wait_for_selector("#detail:not([hidden])")
    assert page.locator("#detailResize").is_visible()


def _assert_pane_closed(page):
    assert not page.locator("#detail").is_visible()
    assert not page.locator("#detailResize").is_visible()


def _back_to_grid(page):
    page.evaluate("() => __winnow.showGridTab()")
    page.wait_for_selector("#grid:not([hidden])")
    page.wait_for_function("() => __winnow.S.activeTab === 'grid'")


def test_the_sql_page_closes_the_pane_and_it_stays_closed_on_return(page):
    _open_pane(page)
    page.locator("#tabSql").click()
    page.wait_for_selector("#sqlview:not([hidden])")
    _assert_pane_closed(page)
    _back_to_grid(page)
    _assert_pane_closed(page)
    # Closed, not broken: the row is still current, so the toggle brings it
    # back on request, and its own Close button still works.
    page.evaluate("() => __winnow.toggleDetailPane()")
    page.wait_for_selector("#detail:not([hidden])")
    page.locator("#btnCloseDetail").click()
    page.wait_for_selector("#detail", state="hidden")
    _assert_pane_closed(page)


def test_the_notes_page_closes_the_pane(page):
    _open_pane(page)
    try:
        page.evaluate("() => __winnow.showNotesTab()")
        page.wait_for_selector("#notesview:not([hidden])")
        _assert_pane_closed(page)
    finally:
        _back_to_grid(page)
    _assert_pane_closed(page)


def test_a_plugin_tab_closes_the_pane(page, fake_plugin_mount):
    # showPluginTab swaps views itself rather than through showMainView, so
    # it is the route a hide placed in showMainView would have missed.
    _open_pane(page)
    page.evaluate("() => { __winnow.showPluginTab('fake.t'); }")
    page.wait_for_function("() => __winnow.S.activeTab === 'plugin:fake.t'")
    _assert_pane_closed(page)


def test_switching_tables_closes_the_pane_without_unbinding_the_note_box(page, server, server_post, tmp_path):
    other = tmp_path / "other.csv"
    other.write_text("Host,User\nh0,u0\nh1,u1\n", encoding="utf-8")
    server_post("/api/ingest/path", {"path": str(other), "build_fts": False})
    page.evaluate("() => { __winnow.loadSources(undefined, { navigate: false }); }")
    page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'other.csv')", timeout=30_000)
    other_id = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'other.csv').id")
    home_id = page.evaluate("() => __winnow.S.sourceId")
    try:
        _open_pane(page)
        bound = page.evaluate(NOTE_BINDING)
        assert bound[0] and bound[1]
        page.evaluate("(id) => { __winnow.openSource(id); }", other_id)
        page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view", arg=other_id, timeout=30_000)
        _assert_pane_closed(page)
        # Hidden, not unbound: the note autosave is a debounce that reads the
        # box's rid/source_id when it fires, so a note typed just before the
        # switch has to post against the row it was typed for.
        assert page.evaluate(NOTE_BINDING) == bound
    finally:
        # Shared server: take the extra table away and put the grid back on
        # the fixture table the other modules expect.
        _delete_source(server, other_id)
        page.evaluate("(id) => { __winnow.loadSources(id); }", home_id)
        page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view", arg=home_id, timeout=30_000)
    assert page.evaluate("() => __winnow.S.sources.some((s) => s.name === 'other.csv')") is False


def test_a_refresh_of_the_same_table_leaves_the_pane_open(page):
    # loadSources() with no select re-enters openSource for the table that is
    # already open. That is the refresh idiom behind adding a column from the
    # pane's own menu, a sidebar folder op and closing some OTHER tab — none
    # of which leaves the table, so none of which should take the pane away.
    _open_pane(page)
    bound = page.evaluate(NOTE_BINDING)
    page.evaluate("() => { window.__viewBefore = __winnow.S.view; __winnow.loadSources(); }")
    # A new S.view is the witness that loadSources went back through openSource.
    page.wait_for_function("() => !!__winnow.S.view && __winnow.S.view !== window.__viewBefore", timeout=30_000)
    assert page.locator("#detail").is_visible()
    assert page.locator("#detailResize").is_visible()
    assert page.evaluate(NOTE_BINDING) == bound


def test_closing_the_last_tab_closes_the_pane(page, server_post):
    # With no tab left, loadSources lands on the empty state rather than on
    # another table — but the pane was still showing a row of the table just
    # closed, note box bound to it.
    home_id = page.evaluate("() => __winnow.S.sourceId")
    _open_pane(page)
    try:
        page.evaluate("() => { __winnow.closeAllTabs(); }")
        page.wait_for_function("() => __winnow.S.sourceId === null && !document.getElementById('empty').hidden", timeout=30_000)
        _assert_pane_closed(page)
    finally:
        # Shared server: give the fixture table its tab back.
        server_post(f"/api/source/{home_id}/open", {"open": True})
        page.evaluate("(id) => { __winnow.loadSources(id); }", home_id)
        page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view", arg=home_id, timeout=30_000)
    assert page.locator("#grid").is_visible()
