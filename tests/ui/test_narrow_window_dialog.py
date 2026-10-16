"""Opening a dialog in a narrow window must not slide the app sideways.

Below some width (~717px on a case with a few tables open, less on the
suite's one-table case) the toolbar's min-content is wider than the
window, so the page overflows to the right — clipped, because html/body
are `overflow: hidden`. But a `hidden` box is still a scroll container:
opening a dialog scrolled <body> sideways to bring what it focused into
view, and nothing ever scrolled it back. The app sat off the left edge
until a reload, widening the window included."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _offsets(page):
    return page.evaluate("""() => ({
      body: document.body.scrollLeft,
      root: document.scrollingElement.scrollLeft,
      app: Math.round(document.getElementById('app').getBoundingClientRect().left),
    })""")


def _narrow_enough_to_overflow(page):
    """The widest window this case's chrome no longer fits in — the regime
    the bug lives in, found rather than hard-coded, since how wide the
    toolbar is depends on the tables open."""
    for width in range(720, 300, -20):
        page.set_viewport_size({"width": width, "height": 700})
        page.wait_for_timeout(100)
        if page.evaluate("() => document.body.scrollWidth > document.body.clientWidth"):
            return width
    pytest.fail("the page never overflowed — nothing here to test")


def test_a_dialog_opened_in_a_narrow_window_leaves_the_app_where_it_was(page, painted):
    try:
        _narrow_enough_to_overflow(page)
        assert _offsets(page) == {"body": 0, "root": 0, "app": 0}
        page.click("#btnSettings")
        page.wait_for_selector("#modal:not([hidden])")
        page.wait_for_timeout(300)   # the dialog focuses its first control on a timer
        assert _offsets(page) == {"body": 0, "root": 0, "app": 0}

        page.keyboard.press("Escape")
        page.set_viewport_size({"width": 1280, "height": 700})
        painted(page)
        assert _offsets(page) == {"body": 0, "root": 0, "app": 0}
    finally:
        page.set_viewport_size({"width": 1500, "height": 900})
