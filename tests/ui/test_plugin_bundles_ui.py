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
