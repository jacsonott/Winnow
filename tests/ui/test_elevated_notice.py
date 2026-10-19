"""The notice a window shows when Winnow runs as administrator on Windows.

Started from an administrator PowerShell, every file dragged in from File
Explorer showed a no-entry cursor, and nothing in the window said why:
Windows refuses the drag before the page gets a single event. The server
can tell (winnow/elevation.py), so the window has to say so. The test
server isn't elevated, so the positive case answers /api/elevation for
it."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui


def _elevated(route):
    route.fulfill(status=200, content_type="application/json",
                  body=json.dumps({"drops_blocked": True}))


def test_an_elevated_winnow_says_why_drops_wont_land(own_page):
    with own_page(route=("**/api/elevation", _elevated), wait_until="load") as pg:
        notice = pg.locator("#elevNotice")
        notice.wait_for(state="visible", timeout=10_000)
        assert "running as administrator" in notice.inner_text()
        assert "Import" in notice.inner_text()   # the way in that still works
        notice.get_by_role("button", name="Got it").click()
        notice.wait_for(state="hidden", timeout=5_000)


def test_a_normal_winnow_shows_nothing(own_page):
    with own_page(wait_until="load") as pg:
        # Asked again and awaited, so the check below runs after the answer
        # (the test server's real one) has been acted on, not before.
        pg.evaluate("() => __winnow.maybeWarnElevated()")
        assert pg.locator("#elevNotice").is_hidden()
