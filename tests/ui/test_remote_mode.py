"""Remote session mode (Settings → Appearance → "Remote session mode").

RDP-class protocols encode screen *changes*: smooth scrolling makes every
wheel notch a dozen full-viewport re-encodes, infinite animations keep the
encoder busy while nothing happens, and the row-hover repaint fires on
every mousemove. The mode swaps smooth scrolling for whole-row jumps and
kills the rest. Its own module — batch PRs must not share a test file's
tail (see the multi-PR conflict lesson)."""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture(autouse=True)
def reset_remote_setting(server):
    """Remote mode is a MACHINE setting now (workspace app_settings), so a
    test that turns it on would decide every later test's outcome — turn
    it back off server-side after each one."""
    yield
    req = urllib.request.Request(
        server.rstrip("/") + "/api/settings/app",
        data=json.dumps({"remote_session": False}).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    urllib.request.urlopen(req, timeout=5).read()


def _enable_via_settings(page):
    page.keyboard.press("?")
    page.wait_for_selector("#modal:not([hidden])")
    page.click(".settings-section-head:has-text('Appearance')")
    page.locator("label:has-text('Remote session mode') input").check()
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_remote_mode_scrolls_by_whole_rows_and_stops_animations(page):
    _enable_via_settings(page)
    assert page.evaluate("() => document.documentElement.classList.contains('remote')")
    # Server-side now — localStorage deliberately does NOT carry the key
    # anymore (it kept getting reset by origin/profile changes). The
    # toggle's POST is async; wait for the response to land.
    page.wait_for_function("() => __winnow.S.appSettings.remote_session === true", timeout=10_000)
    assert not page.evaluate(
        "() => 'remoteSession' in JSON.parse(localStorage.getItem('winnow.appearance') || '{}')")

    row_h = page.evaluate("() => __winnow.ROW_H")
    page.mouse.move(700, 500)
    page.mouse.wheel(0, 100)  # one wheel notch
    page.wait_for_timeout(250)
    st = page.evaluate("() => document.getElementById('body').scrollTop")
    assert st > 0 and st % row_h == 0, f"scrollTop {st} is not a whole-row position (ROW_H={row_h})"
    assert st // row_h == 3, "one notch should jump the Windows-default 3 rows"

    page.mouse.wheel(0, -500)  # back up past the top: clamps, doesn't wrap
    page.wait_for_timeout(250)
    assert page.evaluate("() => document.getElementById('body').scrollTop") == 0

    # The busy bar's infinite sweep is exactly the kind of animation that
    # keeps a remote encoder busy around the clock.
    assert page.evaluate(
        "() => getComputedStyle(document.querySelector('.busy-bar')).animationName") == "none"


def test_remote_mode_makes_row_hover_a_repaint_noop(page):
    box = page.locator(".row").nth(4).bounding_box()
    bg = "() => getComputedStyle(document.querySelectorAll('.row')[4]).backgroundColor"
    page.mouse.move(box["x"] + 300, box["y"] + box["height"] / 2)
    page.wait_for_timeout(100)
    hovered_native = page.evaluate(bg)
    page.mouse.move(box["x"] + 300, box["y"] + 300)  # move away
    page.wait_for_timeout(100)
    resting = page.evaluate(bg)
    assert hovered_native != resting, "hover changes the row color natively, or this proves nothing"

    _enable_via_settings(page)
    page.mouse.move(box["x"] + 300, box["y"] + box["height"] / 2)
    page.wait_for_timeout(100)
    assert page.evaluate(bg) == resting, "in remote mode hovering must not change a single pixel"


def test_native_scrolling_is_untouched_when_off(page):
    assert not page.evaluate("() => document.documentElement.classList.contains('remote')")
    page.mouse.move(700, 500)
    page.mouse.wheel(0, 100)
    page.wait_for_timeout(250)
    # 100px of native scroll — not quantized to the 24/20px row grid.
    assert page.evaluate("() => document.getElementById('body').scrollTop") == 100


def test_remote_mode_survives_a_fresh_browser_context(page, browser, server):
    """The reported bug: the setting lived in per-origin, per-profile
    localStorage, so an update restart (or a quick-look's random port)
    reset it. Machine-side storage means a completely fresh browser
    context — no localStorage at all — still comes up in remote mode."""
    _enable_via_settings(page)
    ctx = browser.new_context()
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.setItem('winnow.appearance', JSON.stringify({ splash: false }))")
    pg = ctx.new_page()
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_function("() => document.documentElement.classList.contains('remote')",
                             timeout=10_000)
    finally:
        ctx.close()


def test_a_pre_move_localstorage_value_migrates_up_once(browser, server):
    """An install that had remote mode on under the old localStorage
    scheme: the first boot pushes it to the server and drops the local
    key, so the machine keeps the analyst's answer."""
    ctx = browser.new_context()
    ctx.add_init_script(
        "localStorage.setItem('winnow.remotePrompt', 'seen');"
        "localStorage.setItem('winnow.appearance',"
        " JSON.stringify({ splash: false, remoteSession: true }))")
    pg = ctx.new_page()
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_function("() => __winnow.S.appSettings.remote_session === true", timeout=10_000)
        assert pg.evaluate("() => document.documentElement.classList.contains('remote')")
        assert not pg.evaluate(
            "() => 'remoteSession' in JSON.parse(localStorage.getItem('winnow.appearance') || '{}')")
    finally:
        ctx.close()
