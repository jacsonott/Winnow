"""Settings → Environment: WINNOW_* variables listed by name only, added
with the prefix filled in, and removed — the value never comes back."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_env_section(page):
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    head = page.locator(".settings-section-head", has_text="Environment")
    if head.get_attribute("aria-expanded") != "true":
        head.click()
    page.wait_for_selector("#modalBody .env-add")


def test_environment_panel_adds_lists_and_removes_without_showing_values(page, api):
    try:
        _open_env_section(page)
        body = page.locator("#modalBody .settings-section", has_text="Environment")
        assert "WINNOW_* names" in body.inner_text()
        assert body.locator(".env-value-in").get_attribute("type") == "password"

        body.locator(".env-name-in").fill("ui_test_key")   # prefix and case are filled in
        body.locator(".env-value-in").fill("hunter2")
        body.locator(".env-add .btn", has_text="Save").click()
        page.wait_for_selector("#modalBody .env-row .env-name:has-text('WINNOW_UI_TEST_KEY')")
        row = body.locator(".env-row", has_text="WINNOW_UI_TEST_KEY")
        assert "saved · active" in row.inner_text()
        assert "hunter2" not in page.locator("#modalBody").inner_html()
        assert "hunter2" not in page.evaluate("() => fetch('/api/env').then((r) => r.text())")
        assert body.locator(".env-value-in").input_value() == ""   # cleared after save

        row.locator(".env-del").click()
        page.wait_for_selector(".confirm-overlay")
        page.locator(".confirm-card .btn", has_text="Remove").click()
        page.wait_for_function(
            "() => !document.querySelector(\"#modalBody .env-row .env-name\")"
            " || ![...document.querySelectorAll('#modalBody .env-row .env-name')].some((n) => n.textContent === 'WINNOW_UI_TEST_KEY')")
        assert all(v["name"] != "WINNOW_UI_TEST_KEY" for v in api("/api/env")["vars"])
    finally:
        page.keyboard.press("Escape")
        api("/api/env/WINNOW_UI_TEST_KEY", "DELETE")


def test_a_reserved_name_is_refused_with_a_toast(page, api):
    try:
        _open_env_section(page)
        body = page.locator("#modalBody .settings-section", has_text="Environment")
        body.locator(".env-name-in").fill("WINNOW_WORKSPACE_DIR")
        body.locator(".env-value-in").fill("/tmp/elsewhere")
        body.locator(".env-add .btn", has_text="Save").click()
        page.wait_for_selector("#toast:not([hidden])")
        assert "Could not save" in page.locator("#toast").inner_text()
    finally:
        page.keyboard.press("Escape")
