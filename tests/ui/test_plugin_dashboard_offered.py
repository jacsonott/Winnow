"""A plugin's board in the sidebar: offered, added by ＋, never applied.

register_dashboard puts a board under Dashboards ▸ Library beside the
machine-wide ones. The rule these drive is that enabling a plugin does
not put anything in the case — the analyst opened Winnow to look at
something, and a plugin does not get to decide what.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui

BOARD = "ESXi / Linux host overview"


def _post(server, route, body):
    req = urllib.request.Request(
        server.rstrip("/") + route, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


@pytest.fixture
def offered(page, server):
    """The bundled esxi_logs plugin, which registers the reference board."""
    _post(server, "/api/plugins/toggle", {"fs_name": "esxi_logs", "scope": "on_all"})
    page.evaluate("() => __winnow.loadPlugins()")
    page.wait_for_function("() => (__winnow.S.pluginDashboards || []).length > 0", timeout=10_000)
    page.evaluate("() => __winnow.renderSidebar()")
    yield
    page.evaluate("""async () => {
      for (const d of __winnow.S.dashboards.filter((x) => x.name.includes('host overview'))) {
        await fetch('/api/dashboards/' + d.id, { method: 'DELETE',
          headers: { 'X-Timeline-Lite-Client': '1' } });
      }
      __winnow.S.dashboardId = null;
      __winnow.showGridTab();
      await __winnow.loadDashboards();
      __winnow.renderSidebar();
    }""")
    _post(server, "/api/plugins/toggle", {"fs_name": "esxi_logs", "scope": "off_all"})
    page.evaluate("() => __winnow.loadPlugins()")


def _row(page):
    return page.locator("#sidebarList .sidebar-dash-library", has_text=BOARD)


def test_enabling_the_plugin_offers_the_board_without_adding_it(page, offered):
    assert _row(page).count() == 1
    assert page.evaluate("() => __winnow.S.dashboards.some((d) => d.name.includes('host overview'))") is False, \
        "loading a plugin must not put a board in the case"


def test_the_row_says_who_offers_it_and_how_big_it_is(page, offered):
    row = _row(page)
    # The plugin's display name ("esxi-logs"), which is what the analyst
    # sees in Settings — not its folder name.
    assert "esxi-logs" in row.locator("button.menu-item").get_attribute("title")
    assert row.locator(".sidebar-row-count").inner_text() == "5"


def test_it_cannot_be_deleted_from_here(page, offered):
    """The plugin owns it — removing it means turning the plugin off, not
    a ✕ that would silently come back on the next load."""
    row = _row(page)
    row.hover()
    assert row.locator(".menu-item-action").count() == 1
    assert row.locator(".menu-item-action[title^='Add this board']").count() == 1


def test_the_plus_copies_it_into_the_case_and_opens_it(page, offered):
    row = _row(page)
    row.hover()
    row.locator(".menu-item-action[title^='Add this board']").click()
    page.wait_for_selector("#dashboardview:not([hidden])")
    page.wait_for_function("() => __winnow.S.dashboards.some((d) => d.name.includes('host overview'))")
    # Five widgets plus the trailing "＋ Add widget" card.
    assert page.locator("#dashboardview .dash-card:not(.dash-add)").count() == 5


def test_adding_it_twice_refreshes_rather_than_duplicating(page, offered):
    row = _row(page)
    for _ in range(2):
        row.hover()
        row.locator(".menu-item-action[title^='Add this board']").click()
        page.wait_for_selector("#dashboardview:not([hidden])")
        page.wait_for_function("() => __winnow.S.dashboards.some((d) => d.name.includes('host overview'))")
    assert page.evaluate(
        "() => __winnow.S.dashboards.filter((d) => d.name.includes('host overview')).length") == 1
