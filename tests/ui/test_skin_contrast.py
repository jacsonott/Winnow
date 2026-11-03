"""Text stays readable on its background, in every skin and theme.

test_skin_tokens.py holds every skin to the same token list and the two
themes to being different; nothing held the colors to being legible
against each other, so a skin edit could put grey text on a grey panel and
every test would stay green. This resolves each pair in the live document
(a token can be a color-mix() or carry alpha, so only the browser knows
what it comes to), lays a translucent background over the page's --ink the
way it is painted, and takes the WCAG contrast ratio.

Body text (--text, on every surface a row or panel can have) is held to
WCAG AA's 4.5:1, which every skin meets with room to spare. So is a filled
accent's label (--accent-fg on --accent-fill), in every skin: the light
themes were 3.3 to 3.9:1 until their accents were darkened a step, and
Studio dark's white on lavender was 3.9:1 until its fill was split from its
text. The accent as text (sort arrows, the selected tab, an active filter's
mark) is held to 4.5:1 in the dark themes and 3.9:1 in the light ones:
Phosphor light's was 1.3:1 before its green was changed, and Blueprint
light's 2.4:1 before its text became a burnt orange beside its
safety-orange fill. The other pairs are held to where the skins are today,
some below AA: --dim's secondary text, a pressed toggle's label
(--accent-fg on --accent-dim, 3.6:1 in Panel dark), danger, diff and
SQL-highlight colors. Those floors stop a skin getting worse; raising them
is a palette change, not a test change.

The default tags are measured too, because one color serves every skin, as
a row stripe and as a pressed tag chip's text: it has to sit between the
darkest panel and the lightest. TA and Suspicious are held to 3:1
everywhere, the most one color can manage on both (TA's old red was 2.3:1
on Jenna dark, Suspicious's old orange 2.0:1 on the light skins); Benign
to where it is today.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from winnow.store import DEFAULT_TAGS

pytestmark = pytest.mark.ui

AA_TEXT = 4.5
BODY_SURFACES = ["--ink", "--panel", "--panel-2", "--panel-3", "--row-even", "--row-hover",
                 "--row-selected", "--cell-selected-bg", "--sel"]

# (foreground, background, painted over, floor). "Painted over" is the
# surface a translucent background sits on, when it is not --ink. A floor
# is one number, or one per theme.
PAIRS = (
    [("--text", bg, None, AA_TEXT) for bg in BODY_SURFACES]
    + [("--dim", bg, None, 3.5) for bg in ("--ink", "--panel", "--panel-2")]
    + [("--accent", bg, None, {"dark": AA_TEXT, "light": 3.9}) for bg in ("--ink", "--panel", "--panel-2")]
    + [("--accent-fg", "--accent-fill", None, AA_TEXT),
       ("--accent-fg", "--accent-dim", None, 3.6),
       ("--danger", "--danger-bg", "--panel", 3.0),
       ("--diff-a", "--diff-a-bg", "--panel", 3.0),
       ("--diff-b", "--diff-b-bg", "--panel", 3.0),
       ("--str", "--ink", None, 3.0),
       ("--num", "--ink", None, 3.0),
       ("--kw", "--ink", None, 3.0),
       ("--col-type", "--panel", None, 3.0)]
)

# A tag's color is a literal, the same in every skin. Each default tag's
# floor, on the surfaces a stripe and a chip sit on.
TAG_FLOORS = {"TA": 3.0, "Suspicious": 3.0, "Benign": 2.8}
TAG_SURFACES = ["--ink", "--panel", "--panel-2", "--row-even"]
TAG_PAIRS = tuple((color, bg, None, TAG_FLOORS[name]) for name, color, _ in DEFAULT_TAGS for bg in TAG_SURFACES)

RATIOS = r"""(pairs) => {
  const probe = document.createElement('div');
  document.body.append(probe);
  const rgba = (token) => {
    probe.style.color = '';
    probe.style.color = token.startsWith('#') ? token : `var(${token})`;
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


def _below_floor(page, skins, pairs):
    low = {}
    for style, theme in skins:
        page.evaluate("([s, t]) => { __winnow.applyStyle(s); __winnow.applyThemeMode(t); }", [style, theme])
        got = page.evaluate(RATIOS, [[fg, bg, base] for fg, bg, base, _ in pairs])
        for (fg, bg, _, floor), ratio in zip(pairs, got):
            floor = floor[theme] if isinstance(floor, dict) else floor
            if ratio < floor:
                low[f"{style}/{theme}: {fg} on {bg}"] = f"{ratio:.2f} < {floor}"
    return low


def test_every_skin_keeps_its_text_readable(page, skins):
    assert _below_floor(page, skins, PAIRS) == {}


def test_the_default_tags_read_on_every_skin(page, skins):
    assert sorted(TAG_FLOORS) == sorted(name for name, _, _ in DEFAULT_TAGS), \
        "a default tag with no floor here"
    assert _below_floor(page, skins, TAG_PAIRS) == {}


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


STYLE_CSS = Path(__file__).resolve().parents[2] / "static" / "style.css"


def test_solid_accent_fills_take_the_fill_token():
    """--accent-fg is measured against --accent-fill, and in the two skins
    that split them (Blueprint light, Studio dark) plain --accent is a
    different color. A rule that paints a solid --accent background would
    put the label on a color nothing here measures: Blueprint light's
    burnt-orange text tone under dark ink is 3.4:1, where the fill it
    should be on is 6.6:1. Tints (color-mix of the accent) carry --text
    and are not fills."""
    css = re.sub(r"/\*.*?\*/", "", STYLE_CSS.read_text(encoding="utf-8"), flags=re.S)
    solid = re.compile(r"background(?:-color)?\s*:\s*var\(--accent\)\s*(?:;|!|$)")
    wrong = [sel.strip() for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css) if solid.search(body)]
    assert not wrong, wrong
