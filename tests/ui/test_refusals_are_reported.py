"""An action the server refuses says so.

Each of these handlers awaited its request with no catch (or a try/finally
with none), so a refusal — a 409 with a reason, a write that failed, a
session or table another window removed meanwhile — was an unhandled
rejection: no toast, and a button that looked as if it had done nothing.
The server's own words are what each test looks for, so it fails again
if the error is swallowed some other way.

Every request here is failed by `page.route`, never by breaking the shared
case, so nothing is left behind for the next module.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui


def _refuse(page, pattern, detail, method, status=500):
    """Fail `method` requests matching `pattern` with `detail`; let every
    other request through."""
    body = json.dumps({"detail": detail})

    def handle(route):
        if route.request.method == method:
            route.fulfill(status=status, content_type="application/json", body=body)
        else:
            route.continue_()
    page.route(pattern, handle)


def _toast(page, text):
    page.wait_for_function(
        "(t) => { const n = document.getElementById('toast'); return !n.hidden && n.textContent.includes(t); }",
        arg=text, timeout=5000)


def _confirm(page, label):
    page.locator(".confirm-card .btn", has_text=label).click()


def test_closing_a_tab_the_server_refuses(page):
    _refuse(page, "**/api/source/*/open", "database is locked", "POST")
    page.locator("#sourceTabs .tab .x").first.click()
    _toast(page, "database is locked")
    assert page.locator("#sourceTabs .tab").count() >= 1, "the tab is still there"


def test_the_tables_manager_reports_a_refused_close_and_remove(page):
    _refuse(page, "**/api/source/*/open", "database is locked", "POST")
    _refuse(page, "**/api/source/*", "It is merged into \"m\" — delete that merge first", "DELETE", 409)
    page.evaluate("() => __winnow.openTablesManager()")
    row = page.locator("#modalBody .tables-row").first
    row.wait_for()
    row.locator(".btn", has_text="Close").click()
    _toast(page, "database is locked")

    row.locator(".tables-more").click()
    page.locator(".menu .menu-item", has_text="Remove…").click()
    _confirm(page, "Remove")
    _toast(page, "delete that merge first")
    assert page.evaluate("() => __winnow.S.sources.length") >= 1
    page.keyboard.press("Escape")


def test_removing_a_case_from_the_list_the_server_refuses(page):
    # The DELETE carries ?path= (which case the list meant), so the
    # pattern has to allow a query string after the id.
    _refuse(page, "**/api/cases/ui-refused?*", "workspace is read-only", "DELETE")
    page.evaluate("() => { __winnow.removeCaseFromList({ id: 'ui-refused', name: 'Refused case', "
                  "path: '/nowhere/refused.db-winnow' }); }")
    _confirm(page, "Remove")
    _toast(page, "workspace is read-only")


def test_deleting_a_profile_the_server_refuses(page, api):
    rec = api("/api/plugin_bundles", "POST", {"name": "UI Refused Profile", "plugins": []})
    try:
        _refuse(page, f"**/api/plugin_bundles/{rec['id']}", "workspace is read-only", "DELETE")
        page.keyboard.press("p")
        page.wait_for_selector(".pm-item", timeout=15_000)
        page.locator(".pm-item", has_text="UI Refused Profile").click()
        page.locator(".pm-del").click()
        _confirm(page, "Delete")
        _toast(page, "workspace is read-only")
        page.keyboard.press("Escape")
    finally:
        page.unroute(f"**/api/plugin_bundles/{rec['id']}")
        api(f"/api/plugin_bundles/{rec['id']}", "DELETE")


def test_saving_a_timeline_config_the_server_refuses(page):
    _refuse(page, "**/api/timeline_templates", "workspace is read-only", "POST")
    page.evaluate("() => __winnow.openTimelineSourceConfig()")
    page.locator("#modalBody .btn", has_text="Save").first.click()
    _toast(page, "workspace is read-only")
    page.keyboard.press("Escape")


def test_deleting_an_import_profile_the_server_refuses(page, api):
    rec = api("/api/import_profiles", "POST", {"name": "UI refused import profile"})
    try:
        _refuse(page, f"**/api/import_profiles/{rec['id']}", "workspace is read-only", "DELETE")
        page.evaluate("(id) => { __winnow.openDirectoryImportModal({ profileId: id }); }", rec["id"])
        page.locator("#modalBody .btn", has_text="Delete profile").click()
        _confirm(page, "Delete")
        _toast(page, "workspace is read-only")
        page.keyboard.press("Escape")
    finally:
        page.unroute(f"**/api/import_profiles/{rec['id']}")
        api(f"/api/import_profiles/{rec['id']}", "DELETE")
