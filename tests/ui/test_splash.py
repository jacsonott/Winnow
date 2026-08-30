"""The launch animation.

Builds its own browser contexts rather than using the shared `page`
fixture, which deliberately turns the splash off — a full-viewport
overlay would otherwise make every click in every other test wait it out.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _ctx(browser, appearance="{}"):
    ctx = browser.new_context(viewport={"width": 1100, "height": 700})
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        f"localStorage.setItem('winnow.appearance', JSON.stringify({appearance}))")
    return ctx


def test_it_plays_on_launch_and_then_reveals_the_app(browser, server):
    ctx = _ctx(browser)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(server)
        # Covering the app while it runs...
        pg.wait_for_selector("#splash:not([hidden])", timeout=10_000)
        assert pg.locator("#splashCanvas").is_visible()
        # ...and gone afterwards, onto a screen that is already populated.
        pg.wait_for_selector("#splash", state="hidden", timeout=30_000)
        pg.wait_for_selector(".row", timeout=15_000)
        assert not errors, errors
    finally:
        ctx.close()


def test_any_key_skips_it(browser, server):
    """An analyst opening their fifth case of the day should never wait."""
    ctx = _ctx(browser)
    pg = ctx.new_page()
    try:
        pg.goto(server)
        pg.wait_for_selector("#splash:not([hidden])", timeout=10_000)
        pg.keyboard.press("Escape")
        # Far faster than the ~4s the animation itself takes to settle.
        pg.wait_for_selector("#splash", state="hidden", timeout=3_000)
    finally:
        ctx.close()


def test_a_click_skips_it(browser, server):
    ctx = _ctx(browser)
    pg = ctx.new_page()
    try:
        pg.goto(server)
        pg.wait_for_selector("#splash:not([hidden])", timeout=10_000)
        pg.mouse.click(550, 350)
        pg.wait_for_selector("#splash", state="hidden", timeout=3_000)
    finally:
        ctx.close()


def test_the_setting_turns_it_off_entirely(browser, server):
    ctx = _ctx(browser, '{ "splash": false }')
    pg = ctx.new_page()
    try:
        pg.goto(server)
        pg.wait_for_selector(".row", timeout=15_000)
        # Never shown at all — not shown-then-hidden.
        assert pg.locator("#splash").get_attribute("hidden") is not None
    finally:
        ctx.close()


def test_it_takes_the_theme_it_is_launched_in(browser, server):
    """A light install must not get a black rectangle thrown at it."""
    shades = {}
    for mode in ("dark", "light"):
        ctx = _ctx(browser, f'{{ "style": "harvest", "themeMode": "{mode}", "accent": "#e0a94a" }}')
        pg = ctx.new_page()
        try:
            pg.goto(server)
            pg.wait_for_selector("#splash:not([hidden])", timeout=10_000)
            shades[mode] = pg.evaluate(
                "() => getComputedStyle(document.getElementById('splash')).backgroundColor")
        finally:
            ctx.close()
    assert shades["dark"] != shades["light"], shades

    def lum(rgb):
        n = [int(x) for x in rgb[rgb.index("(") + 1:rgb.index(")")].split(",")[:3]]
        return 0.299 * n[0] + 0.587 * n[1] + 0.114 * n[2]

    assert lum(shades["light"]) > 200, shades["light"]
    assert lum(shades["dark"]) < 60, shades["dark"]


def test_it_wears_the_skin_that_is_actually_applied(browser, server):
    """The splash reads the live --ink and --accent rather than carrying a
    palette of its own — launching into Phosphor should not flash a wheat
    field first."""
    seen = {}
    for style, accent in (("phosphor", "#39e881"), ("harvest", "#e0a94a")):
        ctx = _ctx(browser, f'{{ "style": "{style}", "themeMode": "dark", "accent": "{accent}" }}')
        pg = ctx.new_page()
        try:
            pg.goto(server)
            pg.wait_for_selector("#splash:not([hidden])", timeout=10_000)
            seen[style] = pg.evaluate("""() => {
              const cs = getComputedStyle(document.documentElement);
              return { bg: getComputedStyle(document.getElementById('splash')).backgroundColor,
                       accent: cs.getPropertyValue('--accent').trim() };
            }""")
        finally:
            ctx.close()
    # Each skin's own background, and its own accent for the grain.
    assert seen["phosphor"]["bg"] != seen["harvest"]["bg"], seen
    assert seen["phosphor"]["accent"].lower().startswith("#39e881")
    assert seen["harvest"]["accent"].lower().startswith("#e0a94a")


def test_the_finished_wordmark_is_held_before_handing_over(browser, server):
    """The grain settling is the payoff; cutting away the moment the last
    one lands throws it away."""
    import time as _t

    ctx = _ctx(browser)
    pg = ctx.new_page()
    try:
        pg.goto(server)
        pg.wait_for_selector("#splashTagline.visible", timeout=25_000)
        shown = _t.monotonic()
        pg.wait_for_selector("#splash", state="hidden", timeout=25_000)
        held = _t.monotonic() - shown
        assert held > 1.5, f"handed over {held:.1f}s after settling — too quick to read"
    finally:
        ctx.close()
