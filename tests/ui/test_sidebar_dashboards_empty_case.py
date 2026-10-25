"""A case with no tables yet still lists its dashboards in the sidebar —
a fresh case with a profile applied already has boards, and an empty one
needs the "New dashboard" row to be reachable. Boots its own server on an
empty case, since the shared fixture's case has tables."""

from __future__ import annotations

import pytest

from spawn import running_server

pytestmark = pytest.mark.ui


@pytest.fixture
def empty_case_server(tmp_path):
    with running_server(tmp_path, "--case", tmp_path / "empty.db", "--no-fts", cwd=tmp_path,
                        env={"WINNOW_IDLE_EXIT_S": "600", "WINNOW_NEVER_CONNECTED_EXIT_S": "600"},
                        ready="/api/sources") as srv:
        yield srv.base


def test_an_empty_case_still_shows_its_dashboards_section(own_page, empty_case_server):
    with own_page(base=empty_case_server, viewport=(1400, 900), ready="#app:not([hidden])") as pg:
        assert pg.evaluate("() => __winnow.S.sources.length") == 0
        pg.wait_for_selector("#sidebarList .menu-header:has-text('Dashboards')")
        assert pg.locator("#sidebarList .menu-item", has_text="New dashboard").count() == 1
        # …and a board created in the empty case appears as a row
        pg.evaluate("""() => fetch('/api/dashboards', { method: 'POST',
            headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: 'Empty-case board' }) })""")
        pg.evaluate("() => __winnow.loadDashboards().then(() => __winnow.renderSidebar())")
        pg.wait_for_selector("#sidebarList .sidebar-row:has-text('Empty-case board')")
