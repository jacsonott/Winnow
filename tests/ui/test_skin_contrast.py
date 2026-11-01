"""Text stays readable on its background, in every skin and theme.

test_skin_tokens.py holds every skin to the same token list and the two
themes to being different; nothing held the colors to being legible
against each other, so a skin edit could put grey text on a grey panel and
every test would stay green. This resolves each pair in the live document
(a token can be a color-mix() or carry alpha, so only the browser knows
what it comes to), lays a translucent background over the page's --ink the
way it is painted, and takes the WCAG contrast ratio.

The floors are of two kinds. Body text (--text, on every surface a row or
panel can have) is held to WCAG AA's 4.5:1, which every skin meets with
room to spare. The other pairs are held to where the skins are today,
which is below AA in some light themes: --dim's secondary text, a primary
button's label (--accent-fg on --accent), error, diff and SQL-highlight
colors. Those floors stop a skin getting worse; raising them is a palette
change, not a test change. The accent used as text (sort arrows, an active
filter's mark) and the default tag colors as swatches are not held here:
Phosphor's light accent is 1.3:1 on its panel, and that wants a design
decision before it wants a floor.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

AA_TEXT = 4.5
BODY_SURFACES = ["--ink", "--panel", "--panel-2", "--panel-3", "--row-even", "--row-hover",
                 "--row-selected", "--cell-selected-bg", "--sel"]

# (foreground, background, painted over, floor). "Painted over" is the
# surface a translucent background sits on, when it is not --ink.
PAIRS = (
    [("--text", bg, None, AA_TEXT) for bg in BODY_SURFACES]
    + [("--dim", bg, None, 3.5) for bg in ("--ink", "--panel", "--panel-2")]
    + [("--accent-fg", "--accent", None, 3.0),
       ("--danger", "--danger-bg", "--panel", 3.0),
       ("--diff-a", "--diff-a-bg", "--panel", 3.0),
       ("--diff-b", "--diff-b-bg", "--panel", 3.0),
       ("--str", "--ink", None, 3.0),
       ("--num", "--ink", None, 3.0),
       ("--kw", "--ink", None, 3.0),
       ("--col-type", "--panel", None, 3.0)]
)

RATIOS = r"""(pairs) => {
  const probe = document.createElement('div');
  document.body.append(probe);
  const rgba = (token) => {
    probe.style.color = '';
    probe.style.color = `var(${token})`;
    const m = getComputedStyle(probe).color.match(/[\d.]+/g).map(Number);
    return [m[0], m[1], m[2], m.length > 3 ? m[3] : 1];
  };
  const over = (top, under) => [0, 1, 2].map((i) => top[i] * top[3] + under[i] * (1 - top[3])).concat([1]);
  const channel = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
  const lum = (c) => 0.2126 * channel(c[0]) + 0.7152 * channel(c[1]) + 0.0722 * channel(c[2]);
  const ink = rgba('--ink');
  const out = [];
  for (const [fg, bg, base] of pairs) {
    const back = over(rgba(bg), base ? over(rgba(base), ink) : ink);
    const front = over(rgba(fg), back);
    const [a, b] = [lum(front), lum(back)];
    out.push((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05));
  }
  probe.remove();
  return out;
}"""


@pytest.fixture
def skins(page):
    page.evaluate("() => { if (__winnow.S.appearance.accentCustomized) throw new Error('accent customized'); }")
    combos = [(s, t) for s in page.evaluate("() => Object.keys(__winnow.STYLES)") for t in ("dark", "light")]
    try:
        yield combos
    finally:
        page.evaluate("() => { __winnow.applyStyle('harvest'); __winnow.applyThemeMode('dark'); }")


def _ratios(page, style, theme):
    page.evaluate("([s, t]) => { __winnow.applyStyle(s); __winnow.applyThemeMode(t); }", [style, theme])
    got = page.evaluate(RATIOS, [[fg, bg, base] for fg, bg, base, _ in PAIRS])
    return {(fg, bg): ratio for (fg, bg, _, _), ratio in zip(PAIRS, got)}


def test_every_skin_keeps_its_text_readable(page, skins):
    low = {}
    for style, theme in skins:
        ratios = _ratios(page, style, theme)
        for fg, bg, _, floor in PAIRS:
            if ratios[(fg, bg)] < floor:
                low[f"{style}/{theme}: {fg} on {bg}"] = f"{ratios[(fg, bg)]:.2f} < {floor}"
    assert low == {}, low


def test_the_measure_tells_light_from_dark(page, skins):
    """Guards the arithmetic from passing everything: black on white is
    21:1, a color on itself 1:1."""
    page.evaluate("() => { document.documentElement.style.setProperty('--zz-a', '#000');"
                  " document.documentElement.style.setProperty('--zz-b', '#fff'); }")
    try:
        same, full = page.evaluate(RATIOS, [["--zz-a", "--zz-a", None], ["--zz-a", "--zz-b", None]])
    finally:
        page.evaluate("() => { const s = document.documentElement.style;"
                      " s.removeProperty('--zz-a'); s.removeProperty('--zz-b'); }")
    assert round(same, 2) == 1.0 and round(full, 1) == 21.0
