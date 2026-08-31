"""Scrollbars carry the theme instead of the OS default — the one
surface the token system didn't reach (a bright grey OS scrollbar down a
dark grid). scrollbar-color inherits from html, so asserting it there
and on the grid body covers every scroller."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_scrollbars_are_themed_not_os_default(page):
    for sel in ("document.documentElement", "document.getElementById('body')"):
        col = page.evaluate(f"() => getComputedStyle({sel}).scrollbarColor")
        assert col and col != "auto", f"{sel}: scrollbar-color fell back to the OS default"
    width = page.evaluate("() => getComputedStyle(document.getElementById('body')).scrollbarWidth")
    assert width == "thin"
