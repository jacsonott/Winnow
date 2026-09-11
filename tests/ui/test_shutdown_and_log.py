"""Shutdown warns when work is in flight; the log opens from the Case
menu (errors used to go only to the terminal)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_shutdown_warns_about_in_flight_work(page):
    # pretend a search-all sweep is running, then hit shutdown
    page.evaluate("() => { __winnow.S.searchAll = { running: true }; }")
    page.evaluate("() => { __winnow.shutdownWinnow(); }")   # opens the confirm (not awaited)
    page.wait_for_selector(".confirm-overlay")
    msg = page.locator(".confirm-overlay .confirm-message").inner_text()
    assert "Still running" in msg and "Search-all" in msg
    # keep the shared server alive
    page.locator(".confirm-actions .btn", has_text="Keep running").click()
    page.evaluate("() => { __winnow.S.searchAll = null; }")


def test_log_opens_from_case_menu(page):
    page.locator("#btnCase").click()
    page.wait_for_selector(".menu")
    page.locator(".menu >> text=Log…").click()
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").inner_text().lower() == "log"
    # Errors | All, and a filter box.
    assert page.locator("#modalBody .vp-seg .btn").all_inner_texts() == ["Errors", "All"]
    assert page.locator("#modalBody .errlog-search").count() == 1
    page.keyboard.press("Escape")
