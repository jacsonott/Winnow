"""When Winnow's server stops answering, say so. The failure mode this
covers: a window left open across a sleep or a crash comes back to a page
whose every request fails — which used to surface as a bare "Failed to
fetch" toast, and as views that looked empty rather than disconnected."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_a_dead_server_reads_as_a_dead_server_not_an_error_code(page):
    """Every catch in the app prints err.message, so that message is what
    an analyst actually sees."""
    page.route("**/api/version", lambda route: route.abort())
    try:
        msg = page.evaluate("""async () => {
          try { await __winnow.api('/api/version'); return 'no error'; }
          catch (e) { return { message: e.message, offline: !!e.offline }; }
        }""")
        assert msg["offline"] is True
        assert "Failed to fetch" not in msg["message"]
        assert "server" in msg["message"]
    finally:
        page.unroute("**/api/version")


def test_the_banner_appears_and_says_what_to_do(page):
    page.route("**/api/version", lambda route: route.abort())
    try:
        page.evaluate("() => __winnow.api('/api/version').catch(() => {})")
        page.wait_for_selector("#connBanner:not([hidden])")
        text = page.locator("#connBanner").inner_text()
        assert "Lost the connection" in text
        assert "start Winnow again" in text
        # Try again while it is still down explains, rather than reloading
        # into a browser error page.
        page.locator("#connBanner .btn", has_text="Try again").click()
        page.wait_for_function(
            "() => document.querySelector('#connBanner .conn-msg').textContent.includes('Still no answer')")
        assert "untouched" in page.locator("#connBanner").inner_text()
    finally:
        page.unroute("**/api/version")
        page.evaluate("() => { const b = document.getElementById('connBanner'); b.hidden = true; }")


def test_the_banner_clears_once_the_server_answers_again(page):
    page.route("**/api/version", lambda route: route.abort())
    page.evaluate("() => __winnow.api('/api/version').catch(() => {})")
    page.wait_for_selector("#connBanner:not([hidden])")
    page.unroute("**/api/version")
    # any successful request is proof the server is back
    page.evaluate("() => __winnow.api('/api/sources')")
    page.wait_for_selector("#connBanner[hidden]", state="attached")


def test_a_dashboard_that_could_not_load_does_not_look_empty(page, api):
    """The sidebar said 22 widgets while the grid offered "＋ Add widget" —
    a board that failed to load must not read as a board with nothing on
    it, or someone rebuilds what was never lost."""
    board = api("/api/dashboards", "POST", {"name": "Gone-server board", "widgets": [
        {"title": "Rows", "source": "sql", "render": "stat", "query": {"sql": "SELECT 1"}}]})
    try:
        page.route(f"**/api/dashboards/{board['id']}", lambda route: route.abort())
        page.evaluate("(id) => __winnow.showDashboard(id)", board["id"])
        page.wait_for_selector("#dashGrid .dash-empty")
        text = page.locator("#dashGrid .dash-empty").inner_text()
        assert "Could not load this dashboard" in text
        assert "safe in the case file" in text
        assert "No widgets yet" not in text
        # and no invitation to rebuild a board that is still there
        assert page.locator("#dashGrid .dash-add").count() == 0
    finally:
        page.unroute(f"**/api/dashboards/{board['id']}")
        api(f"/api/dashboards/{board['id']}", "DELETE")
        page.evaluate("() => { __winnow.resetDashboard(); }")
        page.evaluate("() => __winnow.showGridTab()")
        page.evaluate("() => { const b = document.getElementById('connBanner'); b.hidden = true; }")


def test_opening_a_board_moves_the_sidebar_highlight(page, api):
    """The header named one board while the sidebar highlighted another,
    so an empty board looked like "the board I picked has no widgets"."""
    a = api("/api/dashboards", "POST", {"name": "Board A", "widgets": [
        {"title": "Rows", "source": "sql", "render": "stat", "query": {"sql": "SELECT 1"}}]})
    b = api("/api/dashboards", "POST", {"name": "Board B", "widgets": []})
    try:
        page.evaluate("() => __winnow.loadDashboards().then(() => __winnow.renderSidebar())")
        page.evaluate("(id) => __winnow.showDashboard(id)", a["id"])
        page.wait_for_selector("#dashBar .dash-title:has-text('Board A')")
        page.wait_for_selector("#sidebarList .sidebar-row.active:has-text('Board A')")

        # open the other one: the highlight has to follow, not stay put
        page.evaluate("(id) => __winnow.showDashboard(id)", b["id"])
        page.wait_for_selector("#dashBar .dash-title:has-text('Board B')")
        page.wait_for_selector("#sidebarList .sidebar-row.active:has-text('Board B')")
        assert page.locator("#sidebarList .sidebar-row.active").count() == 1
        # and the empty board it opened is described as empty, not as failed
        assert "No widgets yet" in page.locator("#dashGrid .dash-empty").inner_text()
    finally:
        for d in (a, b):
            api(f"/api/dashboards/{d['id']}", "DELETE")
        page.evaluate("() => { __winnow.resetDashboard(); }")
        page.evaluate("() => __winnow.showGridTab()")
        page.evaluate("() => __winnow.loadDashboards().then(() => __winnow.renderSidebar())")


def test_a_board_that_is_gone_says_so_and_leaves_the_list(page, api):
    d = api("/api/dashboards", "POST", {"name": "Vanishing board", "widgets": []})
    try:
        page.evaluate("() => __winnow.loadDashboards().then(() => __winnow.renderSidebar())")
        api(f"/api/dashboards/{d['id']}", "DELETE")        # deleted behind the UI's back
        page.evaluate("(id) => __winnow.showDashboard(id)", d["id"])
        page.wait_for_selector("#dashGrid .dash-empty")
        text = page.locator("#dashGrid .dash-empty").inner_text()
        assert "no longer in this case" in text
        assert "Try again" not in text                     # retrying a 404 is pointless
        page.wait_for_function(
            "() => !__winnow.S.dashboards.some((b) => b.name === 'Vanishing board')")
    finally:
        page.evaluate("() => { __winnow.resetDashboard(); }")
        page.evaluate("() => __winnow.showGridTab()")
        page.evaluate("() => __winnow.loadDashboards().then(() => __winnow.renderSidebar())")
