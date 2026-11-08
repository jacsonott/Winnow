"""The File associations panel's manual-setup help: collapsed by default,
platform-specific steps, and the install's REAL registered command shown
verbatim (from /api/assoc/types' new `command` field)."""

from __future__ import annotations

import re
import sys

import pytest

pytestmark = pytest.mark.ui


# The server answers for the machine it runs on, which is the one running
# this test. Asserting Linux's steps everywhere failed the nightly's Windows
# job; macOS has no associations to set, so no manual help either.
@pytest.mark.skipif(sys.platform not in ("linux", "win32"),
                    reason="file associations exist on Windows and Linux only")
def test_manual_help_lists_platform_steps_and_the_real_command(page):
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modalBody .settings-nav-item", has_text="File associations").click()
    page.wait_for_selector(".assoc-manual")
    d = page.locator(".assoc-manual")
    # Collapsed by default; opens on click.
    assert d.evaluate("(n) => n.open") is False
    d.locator("summary").click()
    assert d.evaluate("(n) => n.open") is True
    body = d.locator(".assoc-manual-body").inner_text()
    cmd = d.locator(".assoc-cmd").inner_text().strip()
    if sys.platform == "win32":
        # The per-user Open With entry, and Windows' own dialog for the default.
        assert "HKCU" in body
        assert "Choose another app" in body
        # The real command for THIS install, ending in the registry's "%1".
        assert cmd.endswith('server.py --assoc "%1"'), cmd
        assert re.match(r"[A-Za-z]:\\", cmd), cmd   # a real interpreter path, not a placeholder
    else:
        # The xdg-mime + mimeapps.list route.
        assert "xdg-mime default winnow.desktop" in body
        assert "mimeapps.list" in body
        # The real command for THIS install, ending in the .desktop %F field code.
        assert "server.py --assoc %F" in cmd
        assert cmd.startswith("/"), cmd   # a real interpreter path, not a placeholder
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")
