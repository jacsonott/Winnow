"""Case dashboard — add a SQL stat widget through the editor, see it
render a number, and confirm it persists."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_add_sql_widget_renders_and_persists(page):
    page.locator("#tabDashboard").click()
    page.wait_for_selector("#dashboardview:not([hidden])")
    src = page.evaluate("() => __winnow.S.sources.find((s) => !s.is_merge).id")
    page.locator("#dashAddTop").click()
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modal .confirm-input").first.fill("Row count")
    page.locator("#modal select").first.select_option("sql")   # source
    page.locator("#modal .dash-sql").fill(f"SELECT COUNT(*) AS n FROM src_{src}")
    # render defaults to stat
    page.locator("#modal button", has_text="Save widget").click()
    page.wait_for_selector("#modal[hidden]", state="attached")
    # the card renders a stat number
    page.wait_for_function(
        """() => { const c=[...document.querySelectorAll('.dash-card')].find(x=>/Row count/.test(x.textContent));
                   return c && c.querySelector('.dash-stat') && +c.querySelector('.dash-stat').textContent.replace(/,/g,'')>0; }""",
        timeout=10_000)

    # persists across a reload (stored in the case .db)
    page.reload(wait_until="networkidle")
    page.wait_for_selector(".row")
    page.locator("#tabDashboard").click()
    page.wait_for_selector("#dashboardview:not([hidden])")
    page.wait_for_function(
        "() => [...document.querySelectorAll('.dash-card h4')].some(h => h.textContent === 'Row count')",
        timeout=10_000)
    # cleanup: remove the widget so the shared case is left clean
    page.evaluate("""() => fetch('/api/dashboard', { method:'POST',
      headers:{'Content-Type':'application/json','X-Timeline-Lite-Client':'1'},
      body: JSON.stringify({ widgets: [] }) })""")


def test_widget_template_writes_a_working_query(page):
    """Pick a template + a table and the editor writes the SQL for you —
    no need to start from a blank query box."""
    page.locator("#tabDashboard").click()
    page.wait_for_selector("#dashboardview:not([hidden])")
    page.locator("#dashAddTop").click()
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
        """() => { const c=[...document.querySelectorAll('.dash-card')].find(x=>/Rows via template/.test(x.textContent));
                   return c && c.querySelector('.dash-stat') && +c.querySelector('.dash-stat').textContent.replace(/,/g,'')>0; }""",
        timeout=10_000)
    page.evaluate("""() => fetch('/api/dashboard', { method:'POST',
      headers:{'Content-Type':'application/json','X-Timeline-Lite-Client':'1'},
      body: JSON.stringify({ widgets: [] }) })""")
