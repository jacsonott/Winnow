"""Dashboard creator mode gates the three "Add to dashboard" surfaces:
the column-header menu, the row menu and the Filters ▾ dropdown. Off (the
default), none of them mention dashboards; on, all three do."""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _set(server, on):
    req = urllib.request.Request(
        server.rstrip("/") + "/api/case_settings", data=json.dumps({"dashboard_creator": on}).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    urllib.request.urlopen(req, timeout=10).read()


def _header_menu(page):
    page.locator('.hcell[data-col="Host"]').click(button="right")
    page.wait_for_selector(".menu")
    text = page.locator(".menu").inner_text()
    page.keyboard.press("Escape")
    page.wait_for_selector(".menu", state="detached")
    return text


def _row_menu(page):
    page.locator(".row").nth(1).locator(".cell").nth(1).click(button="right")
    page.wait_for_selector(".menu:not(.menu-sub)")
    text = page.locator(".menu:not(.menu-sub)").inner_text()
    page.keyboard.press("Escape")
    page.wait_for_selector(".menu", state="detached")
    return text


def _filters_menu(page):
    page.locator("#btnFilters").click()
    page.wait_for_selector(".menu")
    text = page.locator(".menu").inner_text()
    page.keyboard.press("Escape")
    page.wait_for_selector(".menu", state="detached")
    return text


def test_off_by_default_the_menus_do_not_mention_dashboards(page, server):
    _set(server, False)
    page.evaluate("() => __winnow.loadCaseSettings()")
    assert "dashboard" not in _header_menu(page).lower()
    assert "dashboard" not in _row_menu(page).lower()
    assert "dashboard" not in _filters_menu(page).lower()


def test_the_case_settings_checkbox_turns_the_entries_on(page, server):
    try:
        page.evaluate("() => __winnow.openCaseSettings()")
        page.wait_for_selector("#caseDashboardCreator")
        box = page.locator("#caseDashboardCreator")
        assert not box.is_checked()
        box.check()
        page.wait_for_function("() => __winnow.S.caseSettings.dashboard_creator === '1'")
        page.keyboard.press("Escape")
        page.wait_for_selector("#modal", state="hidden")
        assert "Top values of Host" in _header_menu(page)
        assert "Add to dashboard" in _row_menu(page)
        assert "count of this view" in _filters_menu(page)
        # Reopening shows it checked; unchecking takes them away again.
        page.evaluate("() => __winnow.openCaseSettings()")
        page.wait_for_selector("#caseDashboardCreator")
        assert page.locator("#caseDashboardCreator").is_checked()
        page.locator("#caseDashboardCreator").uncheck()
        page.wait_for_function("() => __winnow.S.caseSettings.dashboard_creator == null")
        page.keyboard.press("Escape")
        page.wait_for_selector("#modal", state="hidden")
        assert "dashboard" not in _header_menu(page).lower()
    finally:
        _set(server, False)
        page.evaluate("() => __winnow.loadCaseSettings()")
