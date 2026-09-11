"""A plugin's board in the sidebar: offered, added by ＋, never applied.

register_dashboard puts a board under Dashboards ▸ Library beside the
machine-wide ones. The rule these drive is that enabling a plugin does
not put anything in the case — the analyst opened Winnow to look at
something, and a plugin does not get to decide what.
"""

from __future__ import annotations

import json
import time
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
    # Deliberately NOT calling renderSidebar() here: toggling a plugin has
    # to redraw the section itself, and calling it by hand was hiding that
    # it did not (found in review).
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
    # …and turning it off takes the row with it, rather than leaving a
    # phantom whose ＋ would 404.
    page.wait_for_function(
        "(n) => !document.querySelector(`#sidebarList .sidebar-dash-library`)"
        " || ![...document.querySelectorAll('#sidebarList .sidebar-dash-library')]"
        ".some((r) => r.textContent.includes(n))", arg=BOARD, timeout=10_000)


def _row(page):
    return page.locator("#sidebarList .sidebar-dash-library", has_text=BOARD)


def _add_calls(page):
    """Collect the status of every POST to the add route.

    Counting boards afterwards cannot tell success from refusal — a 409
    also leaves exactly one board — which is how a broken second add sat
    behind a passing test. The status is the thing to assert."""
    seen: list[int] = []

    def note(r):
        if "/api/plugin_dashboards/" in r.url and r.url.endswith("/add"):
            seen.append(r.status)

    page.on("response", note)
    return seen


def _wait_for_calls(seen, n, timeout=10.0):
    deadline = time.monotonic() + timeout
    while len(seen) < n and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(seen) == n, f"expected {n} add calls, saw {seen}"


def test_enabling_the_plugin_offers_the_board_without_adding_it(page, offered):
    # Drawn by the toggle itself — nothing here redraws the sidebar.
    _row(page).wait_for(state="visible", timeout=10_000)
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


def _click_add(page, row):
    """Click ＋ and wait for the add request it fires. The sidebar row is
    re-rendered by the previous add (loadDashboards → renderSidebar), and
    on CI a click has landed in that gap and fired nothing — not a
    refusal, just a click on a node that was being replaced. So: the
    click is retried until a request is seen, and it's the request's
    STATUS the test is about."""
    for attempt in range(3):
        row.hover()
        try:
            with page.expect_response(
                    lambda r: "/api/plugin_dashboards/" in r.url and r.url.endswith("/add"),
                    timeout=4_000) as resp:
                row.locator(".menu-item-action[title^='Add this board']").click()
            return resp.value.status
        except Exception:   # noqa: BLE001 — a missed click, try again
            if attempt == 2:
                raise


def test_adding_it_twice_refreshes_rather_than_duplicating(page, offered):
    """Both adds must come back 200. The second one is the plugin's own
    copy being refreshed — there is nothing of the analyst's to discard,
    so it must not stop to ask."""
    calls = []
    row = _row(page)
    for _ in range(2):
        calls.append(_click_add(page, row))
        page.wait_for_selector("#dashboardview:not([hidden])")
        page.wait_for_function("() => __winnow.S.dashboards.some((d) => d.name.includes('host overview'))")
    assert calls == [200, 200], "the second add was refused, not refreshed"
    assert page.locator(".confirm-overlay").count() == 0, "it asked about its own copy"
    assert page.evaluate(
        "() => __winnow.S.dashboards.filter((d) => d.name.includes('host overview')).length") == 1
    assert page.locator("#dashboardview .dash-card:not(.dash-add)").count() == 5


def test_clicking_the_name_adds_it_like_the_plus(page, offered):
    """The name and the ＋ are one action. `label.onclick = addToCase` handed
    the click event to the `replace` parameter, which survived JSON as {}
    and came back 422 — the obvious target was the dead one."""
    calls = _add_calls(page)
    _row(page).locator("button.menu-item").click()
    page.wait_for_selector("#dashboardview:not([hidden])")
    page.wait_for_function("() => __winnow.S.dashboards.some((d) => d.name.includes('host overview'))")
    _wait_for_calls(calls, 1)
    assert calls == [200], "clicking the board's name did not add it"
    assert page.locator("#dashboardview .dash-card:not(.dash-add)").count() == 5


def test_a_name_collision_asks_before_replacing(page, offered):
    """The name is the plugin's, so it can collide with a board the analyst
    built. Their widgets must not go without them saying so."""
    page.evaluate("""async () => {
      await fetch('/api/dashboards', { method: 'POST',
        headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: 'ESXi / Linux host overview',
                               widgets: [{ title: 'Mine', source: 'tags', render: 'stat' }] }) });
      await __winnow.loadDashboards(); __winnow.renderSidebar();
    }""")
    row = _row(page)
    row.hover()
    row.locator(".menu-item-action[title^='Add this board']").click()
    page.wait_for_selector(".confirm-overlay")
    assert "no undo" in page.locator(".confirm-card").inner_text()

    page.locator(".confirm-card .btn", has_text="Keep mine").click()
    page.wait_for_selector(".confirm-overlay", state="detached")
    mine = page.evaluate("""async () => {
      const d = __winnow.S.dashboards.find((x) => x.name === 'ESXi / Linux host overview');
      const r = await fetch('/api/dashboards/' + d.id, { headers: { 'X-Timeline-Lite-Client': '1' } });
      return (await r.json()).widgets; }""")
    assert [w["title"] for w in mine] == ["Mine"], "declining replaced them anyway"
