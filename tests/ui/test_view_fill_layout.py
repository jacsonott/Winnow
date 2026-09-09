"""Two layout guarantees: the sidebar slides in UNDER the tab bar (toggling
it never shifts the top chrome), and the full-page views (Notes, Watchlist,
Dashboard) stretch to fill the main area instead of sizing to content —
the Notes editor IS the page, not a strip at the top of an empty pane."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_sidebar_sits_below_the_bar(page):
    assert not page.locator("#sidebar").is_hidden()
    bar = page.locator("header.bar").bounding_box()
    side = page.locator("#sidebar").bounding_box()
    # Bar spans the full width — the sidebar didn't push it right…
    assert bar["x"] < 1
    # …because the sidebar starts below it.
    assert side["y"] >= bar["y"] + bar["height"] - 1

    # Toggling the sidebar must not move the bar (the original complaint).
    page.locator("#btnTabJump").click()
    page.wait_for_selector("#sidebar", state="hidden")
    bar2 = page.locator("header.bar").bounding_box()
    assert abs(bar2["x"] - bar["x"]) < 1
    assert abs(bar2["width"] - bar["width"]) < 1
    page.locator("#btnTabJump").click()
    page.wait_for_selector("#sidebar", state="visible")


def test_notes_editor_fills_the_view(page):
    page.locator("#tabNotes").click()
    page.wait_for_selector("#notesview:not([hidden])")
    main = page.locator(".main-content").bounding_box()
    ed = page.locator("#notesEditor").bounding_box()
    # The editor takes essentially everything below the notes header bar.
    assert ed["height"] > main["height"] * 0.7, (ed, main)


def test_watchlist_and_dashboard_fill_the_view(page):
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    main = page.locator(".main-content").bounding_box()
    wl = page.locator("#watchlistview").bounding_box()
    assert wl["height"] > main["height"] * 0.9, (wl, main)
    body = page.locator("#watchlistview .wl-body").bounding_box()
    assert body["height"] > main["height"] * 0.5, (body, main)
