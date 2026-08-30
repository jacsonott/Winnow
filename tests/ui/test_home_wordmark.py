"""The home screen's wordmark, and Harvest being the default.

The wordmark is the same dot field the launch animation settles into, so
the case list carries the mark the launch just assembled."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _home(browser, server, appearance="{}"):
    ctx = browser.new_context(viewport={"width": 950, "height": 500})
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        f"localStorage.setItem('winnow.appearance', JSON.stringify({appearance}))")
    pg = ctx.new_page()
    pg.goto(server)
    pg.wait_for_selector(".row", timeout=20_000)
    pg.evaluate("() => { __winnow.showHome(); return __winnow.refreshCases(); }")
    pg.wait_for_selector("#home:not([hidden]) .home-head")
    return ctx, pg


def test_the_wordmark_is_drawn_in_the_accent(browser, server):
    ctx, pg = _home(browser, server, '{ "splash": false }')
    try:
        mark = pg.locator(".home-brand-mark")
        assert mark.count() == 1
        box = mark.bounding_box()
        # Bigger than the 13px text it replaced, and actually painted.
        assert box["height"] > 30, box
        assert box["width"] > 100, box
        painted = pg.evaluate("""() => {
          const c = document.querySelector('.home-brand-mark');
          const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
          let lit = 0, sample = null;
          for (let i = 3; i < d.length; i += 4) {
            if (d[i] > 128) { lit++; if (!sample) sample = [d[i - 3], d[i - 2], d[i - 1]]; }
          }
          return { lit, sample };
        }""")
        assert painted["lit"] > 500, painted
        accent = pg.evaluate(
            "() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
        r, g, b = (int(accent.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        # Within rounding of the accent — the dots are drawn in it, not near it.
        assert max(abs(a - c) for a, c in zip(painted["sample"], (r, g, b))) <= 4, (painted, accent)
    finally:
        ctx.close()


def test_light_mode_gets_the_skin_s_own_accent_not_the_dark_one(browser, server):
    """Each skin defines a darker accent for light mode, because the colour
    that reads on near-black is washed out on parchment. Writing the saved
    accent inline made every one of those dead code."""
    seen = {}
    for mode in ("dark", "light"):
        ctx, pg = _home(browser, server, f'{{ "themeMode": "{mode}", "splash": false }}')
        try:
            seen[mode] = pg.evaluate(
                "() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
        finally:
            ctx.close()
    assert seen["dark"] != seen["light"], seen

    def lum(hexstr):
        h = hexstr.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return 0.299 * r + 0.587 * g + 0.114 * b

    assert lum(seen["light"]) < lum(seen["dark"]), seen


def test_a_custom_accent_still_wins(browser, server):
    """Gating the inline value on accentCustomized must not take the
    analyst's own colour away from them."""
    ctx, pg = _home(browser, server,
                    '{ "accent": "#39e881", "accentCustomized": true, "splash": false }')
    try:
        accent = pg.evaluate(
            "() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
        assert accent.lower() == "#39e881"
    finally:
        ctx.close()


def test_harvest_is_the_default_look(browser, server):
    ctx, pg = _home(browser, server, '{ "splash": false }')
    try:
        assert pg.evaluate("() => document.documentElement.getAttribute('data-style')") == "harvest"
        # And it leads the picker, since the list is a menu and the one you
        # are on should be read first.
        assert pg.evaluate("() => Object.keys(__winnow.STYLES)[0]") == "harvest"
    finally:
        ctx.close()


def test_the_brand_mark_leads_the_word_in_its_own_colors(browser, server):
    """The three-bar icon (grain kept, chaff fading) now fronts the
    wordmark — brand colors from the icon itself, NOT theme tokens, so
    the logo and the OS file icon are visibly the same mark in every
    skin. Pinned by sampling the canvas for the bronze and the grey."""
    ctx, pg = _home(browser, server, '{ "splash": false }')
    try:
        pg.wait_for_function(
            """() => {
              const c = document.querySelector('.home-brand-mark');
              if (!c || !c.width) return false;
              const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
              let bronze = false, grey = false;
              for (let i = 0; i < d.length; i += 4) {
                if (d[i + 3] < 100) continue;   // dots this small are all antialias edge
                if (Math.abs(d[i] - 184) < 12 && Math.abs(d[i + 1] - 132) < 12) bronze = true;
                if (Math.abs(d[i] - 195) < 12 && Math.abs(d[i + 1] - 201) < 12) grey = true;
                if (bronze && grey) return true;
              }
              return false;
            }""", timeout=10_000)
    finally:
        ctx.close()
