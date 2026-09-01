"""The M menu: plugin bundles ("case types") — save, list, apply, delete."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_m_opens_and_closes_the_bundles_menu(page):
    page.keyboard.press("M")
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").inner_text().lower() == "plugin bundles"
    page.keyboard.press("M")  # the toggle contract from the same batch
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_save_list_and_delete_a_bundle(page):
    # seed one through the API, then manage it through the UI
    rec = page.evaluate("""() => fetch('/api/plugin_bundles', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'UI Triage', plugins: ['lateral_movement'] }) }).then((r) => r.json())""")
    page.keyboard.press("M")
    page.wait_for_selector(".session-row:has-text('UI Triage')")  # the list loads async
    row = page.locator(".session-row", has_text="UI Triage")
    assert "lateral_movement" in row.inner_text()
    assert row.locator(".btn", has_text="Apply to this case").is_enabled()

    row.locator(".btn", has_text="✕").click()
    page.wait_for_selector(".confirm-overlay")
    page.locator(".confirm-card .btn", has_text="Delete").click()
    page.wait_for_timeout(300)
    assert page.locator(".session-row", has_text="UI Triage").count() == 0
    bundles = page.evaluate("""() => fetch('/api/plugin_bundles',
      { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
    assert all(b["id"] != rec["id"] for b in bundles)
    page.keyboard.press("Escape")


def test_shipped_kape_profile_is_readonly_and_applies(page):
    """The shipped KAPE-triage profile shows in the menu with a 'shipped'
    badge and no delete button, and applying it loads its 9-widget dashboard."""
    page.keyboard.press("M")
    page.wait_for_selector(".session-row:has-text('KAPE triage')")
    row = page.locator(".session-row", has_text="KAPE triage")
    # read-only: a shipped badge, no delete control
    assert row.locator(".bundle-shipped").count() == 1
    assert row.locator(".btn", has_text="✕").count() == 0
    assert row.locator(".btn", has_text="Apply to this case").is_enabled()

    row.locator(".btn", has_text="Apply to this case").click()
    page.wait_for_selector("#modal[hidden]", state="attached")

    # applying creates a NAMED "KAPE triage" dashboard in the sidebar; open it
    page.evaluate("() => __winnow.renderSidebar()")
    page.wait_for_selector("#sidebarList .sidebar-row:has-text('KAPE triage')")
    page.locator("#sidebarList .sidebar-row", has_text="KAPE triage").locator(".menu-item").click()
    page.wait_for_selector("#dashboardview:not([hidden])")
    page.wait_for_function(
        "() => document.querySelectorAll('#dashGrid .dash-card:not(.dash-add)').length === 16", timeout=10_000)

    # cleanup: leave the shared case as we found it
    page.evaluate("""async () => {
      const h = { 'Content-Type':'application/json', 'X-Timeline-Lite-Client':'1' };
      for (const d of await fetch('/api/dashboards', { headers:h }).then(r=>r.json()))
        await fetch('/api/dashboards/' + d.id, { method:'DELETE', headers:h });
      const wl = await fetch('/api/watchlist', { headers:h }).then(r=>r.json());
      for (const i of (wl.indicators||wl)) await fetch('/api/watchlist/' + i.id, { method:'DELETE', headers:h });
    }""")
    page.keyboard.press("Escape")
