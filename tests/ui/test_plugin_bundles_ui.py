"""The profiles manager (Settings → Profiles, or `p`) — save, list, apply, delete.

The list these drove used to be one row per profile with the buttons on
the end of it; they drive the two-pane manager now (tests/ui/
test_profile_manager.py covers what the panes SAY), and applying goes
through the sheet, which is the point of the sheet.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_m_opens_and_closes_the_profile_manager(page):
    page.keyboard.press("M")
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").inner_text().lower() == "profiles"
    page.keyboard.press("M")  # the toggle contract from the same batch
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_save_list_and_delete_a_profile(page):
    # seed one through the API, then manage it through the UI
    rec = page.evaluate("""() => fetch('/api/plugin_bundles', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'UI Triage', plugins: ['lateral_movement'] }) }).then((r) => r.json())""")
    page.keyboard.press("M")
    page.wait_for_selector(".pm-item:has-text('UI Triage')")  # the list loads async
    page.locator(".pm-item", has_text="UI Triage").click()
    assert "lateral_movement" in page.locator('.pm-sec[data-sec="plugins"]').inner_text()
    assert page.locator(".pm-foot .btn", has_text="Apply to this case").is_enabled()

    page.locator(".pm-head-acts .pm-del").click()
    page.wait_for_selector(".confirm-overlay")
    page.locator(".confirm-card .btn", has_text="Delete").click()
    page.wait_for_function("() => !document.querySelector('.pm-item-label')"
                           " || ![...document.querySelectorAll('.pm-item-label')]"
                           ".some((n) => n.textContent === 'UI Triage')", timeout=10_000)
    bundles = page.evaluate("""() => fetch('/api/plugin_bundles',
      { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
    assert all(b["id"] != rec["id"] for b in bundles)
    page.keyboard.press("Escape")


def test_shipped_kape_profile_is_readonly_and_applies(page):
    """The shipped KAPE-triage profile shows in the manager with a 'shipped'
    badge and no delete button, and applying it — through the sheet, which
    now stands between the button and the case — loads every widget the
    profile defines (counted from the shipped defaults, so growing the
    dashboard doesn't silently stale this test again)."""
    from winnow import defaults
    expected = len(next(pr for pr in defaults.profiles() if pr["name"] == "KAPE triage")["dashboard"])
    page.keyboard.press("M")
    page.wait_for_selector(".pm-item:has-text('KAPE triage')")
    page.locator(".pm-item", has_text="KAPE triage").click()
    # read-only: a shipped badge, no delete control
    assert page.locator(".pm-item", has_text="KAPE triage").locator(".pm-tag")\
        .first.inner_text().lower() == "shipped"   # the badge is uppercased by CSS
    assert page.locator(".pm-head-acts .pm-del").count() == 0
    apply_btn = page.locator(".pm-foot .btn", has_text="Apply to this case")
    assert apply_btn.is_enabled()

    apply_btn.click()
    page.wait_for_selector('.ap-part[data-part="boards"]', timeout=15_000)
    assert f"Create “KAPE triage” ({expected} widgets)" in \
        page.locator('.ap-part[data-part="boards"]').inner_text()
    page.locator(".row-actions .btn", has_text="Apply").click()
    page.wait_for_selector("#modal[hidden]", state="attached", timeout=30_000)

    # applying creates ONE named "KAPE triage" dashboard in the sidebar
    # (the host overview it used to ship beside is folded into it); open it
    page.evaluate("() => __winnow.renderSidebar()")
    page.wait_for_selector("#sidebarList .sidebar-row:has-text('KAPE triage')")
    assert page.locator("#sidebarList .sidebar-row", has_text="KAPE host overview").count() == 0
    page.locator("#sidebarList .sidebar-row", has_text="KAPE triage").locator(".menu-item").click()
    page.wait_for_selector("#dashboardview:not([hidden])")
    page.wait_for_function(
        "(n) => document.querySelectorAll('#dashGrid .dash-card:not(.dash-add)').length === n",
        arg=expected, timeout=10_000)

    # cleanup: leave the shared case as we found it
    page.evaluate("""async () => {
      const h = { 'Content-Type':'application/json', 'X-Timeline-Lite-Client':'1' };
      for (const d of await fetch('/api/dashboards', { headers:h }).then(r=>r.json()))
        await fetch('/api/dashboards/' + d.id, { method:'DELETE', headers:h });
      const wl = await fetch('/api/watchlist', { headers:h }).then(r=>r.json());
      for (const i of (wl.indicators||wl)) await fetch('/api/watchlist/' + i.id, { method:'DELETE', headers:h });
    }""")
    page.keyboard.press("Escape")
