"""The File associations panel's manual-setup help: collapsed by default,
platform-specific steps, and the install's REAL registered command shown
verbatim (from /api/assoc/types' new `command` field)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_manual_help_lists_platform_steps_and_the_real_command(page):
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator(".settings-section-head", has_text="File associations").click()
    page.wait_for_selector(".assoc-manual")
    d = page.locator(".assoc-manual")
    # Collapsed by default; opens on click.
    assert d.evaluate("(n) => n.open") is False
    d.locator("summary").click()
    assert d.evaluate("(n) => n.open") is True
    body = d.locator(".assoc-manual-body").inner_text()
    # Linux CI/dev box → the xdg-mime + mimeapps.list route.
    assert "xdg-mime default winnow.desktop" in body
    assert "mimeapps.list" in body
    # The real command for THIS install, ending in the .desktop %F field code.
    cmd = d.locator(".assoc-cmd").inner_text()
    assert "server.py --assoc %F" in cmd
    assert cmd.strip().startswith("/")   # a real interpreter path, not a placeholder
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")
