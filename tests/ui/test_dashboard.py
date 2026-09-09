"""Named case dashboards: create a board, add a widget through the editor
(with a live preview), see it render and persist, and manage boards from
the sidebar."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _new_board(page, name):
    did = page.evaluate("""async (name) => {
      const h = { 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' };
      const d = await fetch('/api/dashboards', { method: 'POST', headers: h,
        body: JSON.stringify({ name }) }).then(r => r.json());
      await __winnow.loadDashboards();
      await __winnow.showDashboard(d.id);
      return d.id;
    }""", name)
    page.wait_for_selector("#dashboardview:not([hidden])")
    return did


def _delete_board(page, did):
    page.evaluate("""(id) => fetch('/api/dashboards/' + id,
      { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } })""", did)


def test_add_widget_previews_renders_and_persists(page):
    did = _new_board(page, "Board A")
    src = page.evaluate("() => __winnow.S.sources.find((s) => !s.is_merge).id")
    page.locator("#dashBar button", has_text="Add widget").click()
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modal .confirm-input").first.fill("Row count")
    page.locator("#modal select").first.select_option("sql")   # data source
    page.locator("#modal .dash-sql").fill(f"SELECT COUNT(*) AS n FROM src_{src}")
    # preview renders the number BEFORE saving
    page.locator("#modal button", has_text="Preview").click()
    page.wait_for_function(
        "() => { const p = document.querySelector('#modal .dash-preview .dash-stat');"
        "        return p && +p.textContent.replace(/,/g,'') > 0; }", timeout=10_000)
    page.locator("#modal button", has_text="Save widget").click()
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.wait_for_function(
        """() => { const c=[...document.querySelectorAll('#dashGrid .dash-card')].find(x=>/Row count/.test(x.textContent));
                   return c && c.querySelector('.dash-stat') && +c.querySelector('.dash-stat').textContent.replace(/,/g,'')>0; }""",
        timeout=10_000)
    # persists in the case .db across a reload
    page.reload(wait_until="networkidle")
    page.wait_for_selector(".row")
    page.evaluate("(id) => __winnow.showDashboard(id)", did)
    page.wait_for_function(
        "() => [...document.querySelectorAll('#dashGrid .dash-card h4')].some(h => h.textContent === 'Row count')",
        timeout=10_000)
    _delete_board(page, did)


def test_widget_template_writes_a_working_query(page):
    did = _new_board(page, "Board B")
    page.locator("#dashBar button", has_text="Add widget").click()
    page.wait_for_selector("#modal:not([hidden])")
    selects = page.locator("#modal select")
    selects.nth(1).select_option("count")     # template: Total row count
    selects.nth(2).select_option(index=0)     # first real table (src_<id>)
    filled = page.locator("#modal .dash-sql").input_value()
    assert "COUNT(*)" in filled and "FROM src_" in filled
    page.locator("#modal .confirm-input").first.fill("Rows via template")
    page.locator("#modal button", has_text="Save widget").click()
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.wait_for_function(
        """() => { const c=[...document.querySelectorAll('#dashGrid .dash-card')].find(x=>/Rows via template/.test(x.textContent));
                   return c && c.querySelector('.dash-stat'); }""",
        timeout=10_000)
    _delete_board(page, did)


def test_dashboards_listed_in_sidebar_and_deletable(page):
    did = _new_board(page, "Sidebar Board")
    page.evaluate("() => __winnow.renderSidebar()")
    page.wait_for_selector("#sidebarList .menu-header:has-text('Dashboards')")
    row = page.locator("#sidebarList .sidebar-row", has_text="Sidebar Board")
    assert row.count() == 1
    # opening from the sidebar shows the board
    row.locator(".menu-item").click()
    page.wait_for_function("(id) => __winnow.S.dashboardId === id", arg=did)
    _delete_board(page, did)
    page.evaluate("async () => { await __winnow.loadDashboards(); __winnow.renderSidebar(); }")
    page.wait_for_function(
        "() => ![...document.querySelectorAll('#sidebarList .sidebar-row')].some(r => /Sidebar Board/.test(r.textContent))")
