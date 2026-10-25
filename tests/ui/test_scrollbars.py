"""Scrollbars carry the theme instead of the OS default — the one
surface the token system didn't reach (a bright grey OS scrollbar down a
dark grid). scrollbar-color inherits from html, so asserting it there
covers every scroller that uses the standard properties.

The grid's own two are the exception. They are drawn from
::-webkit-scrollbar rules (style.css .vbar — the vertical one is #vbar,
which starts below the header), and Chromium only reads those where
scrollbar-width and scrollbar-color are `auto`, so for them the computed
style is `auto` by design and says nothing about the colour. That is read
off the screen instead, in a browser that draws scrollbars."""

from __future__ import annotations

import base64
import re

import pytest

pytestmark = pytest.mark.ui


def test_scrollbars_are_themed_not_os_default(page):
    col = page.evaluate("() => getComputedStyle(document.documentElement).scrollbarColor")
    assert col and col != "auto", "html: scrollbar-color fell back to the OS default"
    width = page.evaluate("() => getComputedStyle(document.body).scrollbarWidth")
    assert width == "thin"


# At the top-left of the table each thumb starts at its track's start, so
# a point just inside that end is thumb. --line-2 is resolved through an
# element because the token itself may be written any way CSS allows.
THUMBS = """() => {
  const v = document.getElementById('vbar').getBoundingClientRect();
  const b = document.getElementById('body'), bb = b.getBoundingClientRect();
  const probe = document.createElement('div');
  probe.style.color = 'var(--line-2)';
  document.body.append(probe);
  const want = getComputedStyle(probe).color;
  probe.remove();
  return { want,
           vertical: [Math.round((v.left + v.right) / 2), Math.round(v.top + 8)],
           horizontal: [Math.round(bb.left + 24),
                        Math.round(bb.bottom - (b.offsetHeight - b.clientHeight) / 2)] };
}"""

# A screenshot's pixel, decoded by the page itself rather than by an image
# library the dev requirements don't carry.
PIXEL = """async (b64) => {
  const img = new Image();
  img.src = 'data:image/png;base64,' + b64;
  await img.decode();
  const c = document.createElement('canvas');
  c.width = img.width; c.height = img.height;
  const ctx = c.getContext('2d');
  ctx.drawImage(img, 0, 0);
  return Array.from(ctx.getImageData(0, 0, 1, 1).data.slice(0, 3));
}"""


def test_the_grids_scrollbars_wear_the_theme(own_page, scrollbar_browser):
    with own_page(browser=scrollbar_browser, viewport=(1100, 700)) as pg:
        t = pg.evaluate(THUMBS)
        want = [int(n) for n in re.findall(r"\d+", t["want"])[:3]]
        for which in ("vertical", "horizontal"):
            x, y = t[which]
            png = pg.screenshot(clip={"x": x, "y": y, "width": 1, "height": 1})
            got = pg.evaluate(PIXEL, base64.b64encode(png).decode())
            assert all(abs(a - b) <= 3 for a, b in zip(got, want)), (which, got, want)
