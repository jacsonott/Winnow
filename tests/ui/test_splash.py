"""The launch animation.

Builds its own browser contexts rather than using the shared `page`
fixture, which deliberately turns the splash off — a full-viewport
overlay would otherwise make every click in every other test wait it out.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _hex(v: str) -> tuple[int, int, int]:
    h = v.strip().lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb(v: str) -> tuple[int, int, int]:
    """"rgb(15, 18, 17)" as the same triple _hex returns."""
    import re
    return tuple(int(n) for n in re.findall(r"\d+", v)[:3])


def _ctx(own_page, appearance="{}"):
    """A page that has not loaded yet, so the test sees the launch."""
    return own_page(seed="localStorage.setItem('winnow.remotePrompt', 'seen');"
                         f"localStorage.setItem('winnow.appearance', JSON.stringify({appearance}))",
                    viewport=(1100, 700), goto=False)


def test_it_plays_on_launch_and_then_reveals_the_app(own_page, server):
    with _ctx(own_page) as pg:
        pg.goto(server)
        # Covering the app while it runs...
        pg.wait_for_selector("#splash:not([hidden])", timeout=10_000)
        assert pg.locator("#splashCanvas").is_visible()
        # ...and gone afterwards, onto a screen that is already populated.
        pg.wait_for_selector("#splash", state="hidden", timeout=30_000)
        pg.wait_for_selector(".row", timeout=15_000)


def test_any_key_skips_it(own_page, server):
    """An analyst opening their fifth case of the day should never wait."""
    with _ctx(own_page) as pg:
        pg.goto(server)
        pg.wait_for_selector("#splash:not([hidden])", timeout=10_000)
        pg.wait_for_timeout(800)          # past the focus-click grace period
        pg.keyboard.press("Escape")
        # Far faster than the ~4s the animation itself takes to settle.
        pg.wait_for_selector("#splash", state="hidden", timeout=3_000)


def test_a_click_skips_it(own_page, server):
    with _ctx(own_page) as pg:
        pg.goto(server)
        pg.wait_for_selector("#splash:not([hidden])", timeout=10_000)
        pg.wait_for_timeout(800)          # past the focus-click grace period
        pg.mouse.click(550, 350)
        pg.wait_for_selector("#splash", state="hidden", timeout=3_000)


def test_the_setting_turns_it_off_entirely(own_page, server):
    with _ctx(own_page, '{ "splash": false }') as pg:
        pg.goto(server)
        pg.wait_for_selector(".row", timeout=15_000)
        # Never shown at all — not shown-then-hidden.
        assert pg.locator("#splash").get_attribute("hidden") is not None


def test_it_takes_the_theme_it_is_launched_in(own_page, server):
    """A light install must not get a black rectangle thrown at it."""
    shades = {}
    for mode in ("dark", "light"):
        with _ctx(own_page, f'{{ "style": "harvest", "themeMode": "{mode}", "accent": "#d9a441" }}') as pg:
            pg.goto(server)
            pg.wait_for_selector("#splash:not([hidden])", timeout=10_000)
            shades[mode] = pg.evaluate(
                "() => getComputedStyle(document.getElementById('splash')).backgroundColor")
    assert shades["dark"] != shades["light"], shades

    def lum(rgb):
        n = [int(x) for x in rgb[rgb.index("(") + 1:rgb.index(")")].split(",")[:3]]
        return 0.299 * n[0] + 0.587 * n[1] + 0.114 * n[2]

    assert lum(shades["light"]) > 200, shades["light"]
    assert lum(shades["dark"]) < 60, shades["dark"]


def test_it_wears_the_skin_that_is_actually_applied(own_page, server):
    """The splash reads the live --ink and --accent rather than carrying a
    palette of its own — launching into Phosphor should not flash a wheat
    field first."""
    seen = {}
    for style in ("phosphor", "harvest"):
        with _ctx(own_page, f'{{ "style": "{style}", "themeMode": "dark" }}') as pg:
            pg.goto(server)
            pg.wait_for_selector("#splash:not([hidden])", timeout=10_000)
            seen[style] = pg.evaluate("""() => {
              const cs = getComputedStyle(document.documentElement);
              return { bg: getComputedStyle(document.getElementById('splash')).backgroundColor,
                       ink: cs.getPropertyValue('--ink').trim(),
                       accent: cs.getPropertyValue('--accent').trim() };
            }""")

    # Asserted as relationships, not literals. This used to pin each skin's
    # accent by hex, which made it a second copy of the palette: softening
    # Harvest for long sessions failed a splash test, which is the tail
    # wagging the dog. What matters is that the splash wears whatever the
    # skin says, and that two skins do not look the same.
    assert seen["phosphor"]["bg"] != seen["harvest"]["bg"], seen
    assert seen["phosphor"]["accent"] != seen["harvest"]["accent"], seen
    for style, s in seen.items():
        assert _rgb(s["bg"]) == _hex(s["ink"]), (style, s)   # the skin's own ink, not a palette of its own
    pr, pg_, pb = _hex(seen["phosphor"]["accent"])
    hr, hg, hb = _hex(seen["harvest"]["accent"])
    assert pg_ > pr and pg_ > pb, seen["phosphor"]        # phosphor's grain is green
    assert hr > hb and hg > hb, seen["harvest"]           # harvest's is warm


def test_the_finished_wordmark_is_held_before_handing_over(own_page, server):
    """The grain settling is the payoff; cutting away the moment the last
    one lands throws it away."""
    import time as _t

    with _ctx(own_page) as pg:
        pg.goto(server)
        pg.wait_for_selector("#splashTagline.visible", timeout=25_000)
        shown = _t.monotonic()
        pg.wait_for_selector("#splash", state="hidden", timeout=25_000)
        held = _t.monotonic() - shown
        assert held > 1.5, f"handed over {held:.1f}s after settling — too quick to read"


def test_a_click_in_the_first_moments_does_not_skip_it(own_page, server):
    """The click that focuses a freshly opened app window lands on frame
    one — it used to end the animation before anyone saw it."""
    with _ctx(own_page, '{}') as pg:
        pg.goto(server, wait_until="commit")
        pg.wait_for_selector("#splash:not([hidden])", timeout=10_000)
        pg.mouse.click(550, 350)
        pg.wait_for_timeout(300)   # long enough for a skip to have hidden it
        assert pg.locator("#splash").get_attribute("hidden") is None   # still playing


def _wordmark_on_screen(pg):
    """Where the settled wordmark is, read off the canvas in CSS pixels: from
    the brand mark's first bar (its own amber) to the letters' far edge (the
    skin's accent), and the letters' top to bottom."""
    return pg.evaluate("""() => {
      const c = document.getElementById('splashCanvas');
      const a = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim().slice(1);
      const [ar, ag, ab] = [0, 2, 4].map((i) => parseInt(a.slice(i, i + 2), 16));
      const k = c.width / c.clientWidth;
      const { data, width, height } = c.getContext('2d').getImageData(0, 0, c.width, c.height);
      const near = (i, r, g, b) => data[i + 3] > 128 &&
        Math.abs(data[i] - r) + Math.abs(data[i + 1] - g) + Math.abs(data[i + 2] - b) < 40;
      let left = Infinity, right = -1, top = Infinity, bottom = -1;
      for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
          const i = (y * width + x) * 4;
          if (near(i, 184, 132, 58)) left = Math.min(left, x);
          if (near(i, ar, ag, ab)) {
            right = Math.max(right, x); top = Math.min(top, y); bottom = Math.max(bottom, y);
          }
        }
      }
      return { left: left / k, right: right / k, top: top / k, bottom: bottom / k,
               canvas: [c.clientWidth, c.clientHeight], window: [innerWidth, innerHeight] };
    }""")


def test_a_window_resized_mid_launch_gets_the_wordmark_laid_out_for_it(own_page, server):
    """Maximising the window a second into the launch left the canvas at its
    launch size: the word stayed where the small window had centred it, the
    chaff was cut off partway across, and the tagline (placed by CSS, so it
    did move) no longer sat under the word. Shrinking it ran the word off
    the edge. The scene now maps onto the window as it is, centred and
    scaled the way a launch at that size would lay it out — including a
    resize while the finished word is held, when nothing else is drawing."""
    # Phosphor: a green accent nothing else on the canvas is close to.
    with _ctx(own_page, '{ "style": "phosphor", "themeMode": "dark" }') as pg:
        pg.goto(server, wait_until="commit")
        pg.wait_for_selector("#splash:not([hidden])", timeout=10_000)
        pg.wait_for_timeout(400)          # some grain in the air, some not thrown yet
        pg.set_viewport_size({"width": 1600, "height": 1000})
        pg.wait_for_selector("#splashTagline.visible", timeout=25_000)
        grown = _wordmark_on_screen(pg)
        pg.set_viewport_size({"width": 900, "height": 600})
        # Only for the resize event to land: the handler repaints there and
        # then, and the old code changes nothing, so there's no signal to await.
        pg.wait_for_timeout(150)
        shrunk = _wordmark_on_screen(pg)

    for box in (grown, shrunk):
        w, h = box["window"]
        assert box["canvas"] == [w, h], box
        assert abs((box["left"] + box["right"]) / 2 - w / 2) < 20, box     # centred across
        # The word's line is at 44% of the height; the capitals' ink sits a
        # font-dependent ~0.1em off it (23px at 190px on CI's fonts).
        assert abs((box["top"] + box["bottom"]) / 2 - h * 0.44) < h * 0.05, box
    # Sized for the window too: the font is 13% of the width, capped at 190px.
    ratio = (shrunk["right"] - shrunk["left"]) / (grown["right"] - grown["left"])
    assert abs(ratio - 900 * 0.13 / 190) < 0.05, ratio


# Edge on Windows, as the app window reports itself. Winnow reads the
# platform, so both are set; Playwright's user_agent alone leaves
# navigator.platform saying Linux.
WINDOWS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0")


def _reduced_motion_ctx(own_page, appearance, windows=False):
    return own_page(seed="localStorage.setItem('winnow.remotePrompt', 'seen');"
                         f"localStorage.setItem('winnow.appearance', JSON.stringify({appearance}))",
                    init=("Object.defineProperty(Navigator.prototype, 'platform', { get: () => 'Win32' })"
                          if windows else ""),
                    viewport=(1200, 800), goto=False, reduced_motion="reduce",
                    **({"user_agent": WINDOWS_UA} if windows else {}))


def _splash_box(pg):
    pg.evaluate("() => __winnow.openSettings()")
    pg.wait_for_selector("#modal:not([hidden])")
    pg.click("#modal .settings-nav-item:has-text('Appearance')")
    box = pg.locator("#modal label.check-row", has_text="Launch animation").locator("input")
    box.wait_for()
    return box


def _splash_note(pg):
    return pg.locator("#modal .fb-help", has_text="winnowing animation").inner_text()


def test_reduced_motion_skips_by_default_but_an_explicit_tick_plays(own_page, server):
    """Off Windows, reduced motion is an accessibility switch someone chose:
    the default honours it (and Settings says so), a hand-ticked box
    overrides."""
    with _reduced_motion_ctx(own_page, "{ splash: true }") as pg:
        pg.goto(server)
        pg.wait_for_selector(".row")
        assert pg.locator("#splash").get_attribute("hidden") is not None
        last = pg.evaluate("() => JSON.parse(localStorage.getItem('winnow.splash.last'))")
        assert last["result"] == "skipped" and "reduced motion" in last["reason"]
        # Settings explains it, and the box says it is not playing — a ticked
        # box over a skipped animation is what had analysts unticking and
        # re-ticking it to get it back.
        box = _splash_box(pg)
        assert "reduced motion, so it is skipped" in _splash_note(pg)
        assert not box.is_checked()
        box.check()
        assert pg.evaluate("() => __winnow.S.appearance.splash") == "always"
        assert "plays anyway" in _splash_note(pg)
    with _reduced_motion_ctx(own_page, "{ splash: 'always' }") as pg2:
        pg2.goto(server, wait_until="commit")
        pg2.wait_for_selector("#splash:not([hidden])", timeout=10_000)


@pytest.mark.parametrize("appearance", ["{}", "{ splash: true }"])
def test_windows_with_animation_effects_off_still_plays_it_by_default(own_page, server, appearance):
    """Chromium on Windows reports reduced motion whenever Animation effects
    are off, which VM images, RDP profiles and performance settings leave
    that way. Honouring it meant the animation only ever played for analysts
    who had unticked and re-ticked the box. `{}` is an install that never
    saved a look; `splash: true` is one that saved any other appearance
    setting, which writes the default out with it."""
    with _reduced_motion_ctx(own_page, appearance, windows=True) as pg:
        pg.goto(server, wait_until="commit")
        pg.wait_for_selector("#splash:not([hidden])", timeout=10_000)
        pg.wait_for_timeout(800)          # past the focus-click grace period
        pg.keyboard.press("Escape")
        pg.wait_for_selector("#splash", state="hidden", timeout=3_000)
        assert _splash_box(pg).is_checked()
        assert "Animation effects off" in _splash_note(pg)


def test_off_on_windows_stays_off(own_page, server):
    with _reduced_motion_ctx(own_page, "{ splash: false }", windows=True) as pg:
        pg.goto(server)
        pg.wait_for_selector(".row", timeout=15_000)
        assert pg.locator("#splash").get_attribute("hidden") is not None
        assert not _splash_box(pg).is_checked()


def test_a_slow_display_still_finishes_in_seconds(own_page, server):
    """The launch must take the same few seconds on a VM with no GPU and a
    big high-DPI screen as on a workstation. Modelled here with software
    canvas, a 2560×1440 2× viewport and 8× CPU throttling — the shape of
    machine that reported a 30-second 'laggy' launch. The physics ticks on
    a clock, tokens come from a pre-rendered atlas, and the render scale
    drops when frames run slow, so the whole thing stays under ~half of
    what a per-frame simulation took."""
    import time
    with own_page(seed="localStorage.setItem('winnow.remotePrompt', 'seen');"
                       "localStorage.setItem('winnow.appearance', JSON.stringify({ splash: 'always' }))",
                  viewport=(2560, 1440), device_scale_factor=2, goto=False) as pg:
        cdp = pg.context.new_cdp_session(pg)
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": 8})
        pg.goto(server)
        pg.wait_for_selector("#splash:not([hidden])", timeout=20_000)
        t0 = time.monotonic()
        pg.wait_for_selector("#splash[hidden]", state="attached", timeout=60_000)
        took = time.monotonic() - t0
        assert took < 18, f"splash took {took:.1f}s under throttling"


def _set_remote_session(server, on):
    import json
    import urllib.request
    req = urllib.request.Request(
        server.rstrip("/") + "/api/settings/app",
        data=json.dumps({"remote_session": on}).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    urllib.request.urlopen(req, timeout=5).read()


def test_remote_session_mode_skips_it_unless_ticked(own_page, server):
    """Remote session mode promises to stop animations, and a full-viewport
    one is the worst case for a remote display: every frame is re-encoded
    whole, so it arrives choppy however fast the machine draws it. It
    played anyway, because the page learned the (machine-side) setting
    only after the animation had started. A hand-ticked box still plays."""
    _set_remote_session(server, True)
    try:
        with _ctx(own_page) as pg:
            pg.goto(server)
            # The splash runs alongside boot(), so had it started it would
            # still be up when the grid paints.
            pg.wait_for_selector(".row", timeout=15_000)
            assert pg.locator("#splash").get_attribute("hidden") is not None
            last = pg.evaluate("() => JSON.parse(localStorage.getItem('winnow.splash.last'))")
            assert last["result"] == "skipped" and "Remote session mode" in last["reason"]
            box = _splash_box(pg)
            assert not box.is_checked()
            assert "Remote session mode is on, so it is skipped" in _splash_note(pg)
            box.check()
            assert pg.evaluate("() => __winnow.S.appearance.splash") == "always"
            assert "plays anyway" in _splash_note(pg)
        with _ctx(own_page, "{ splash: 'always' }") as pg2:
            pg2.goto(server, wait_until="commit")
            pg2.wait_for_selector("#splash:not([hidden])", timeout=10_000)
    finally:
        _set_remote_session(server, False)
