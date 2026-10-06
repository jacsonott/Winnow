"""Every skin defines every token, in both themes.

A skin is one block of custom properties per theme, and a token left out
of one does not fail loudly — it inherits whatever `:root` or the cascade
happens to leave, so the surface it names comes out wrong in a way only a
screenshot shows. That is the whole failure mode a new skin has, and it
is cheap to rule out: resolve each token in the live document, for every
style the picker offers, under both themes.

Also pinned here because it is the one thing a skin can get structurally
wrong rather than merely ugly: the two themes have to actually differ,
and the grid has to stay monospace.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# The contract every skin block fills in. Taken from the shape the
# existing skins share; a token added to one skin and not the rest shows
# up here as a failure on the others, which is the point.
TOKENS = [
    "--ink", "--panel", "--panel-2", "--panel-3",
    "--line", "--line-2", "--text", "--dim",
    "--accent", "--accent-dim", "--accent-fg",
    "--sel", "--sel-line",
    "--row-even", "--row-hover", "--row-selected", "--cell-selected-bg",
    "--border-hairline", "--text-faint",
    "--danger", "--danger-bg", "--danger-border",
    "--diff-a", "--diff-a-bg", "--diff-b", "--diff-b-bg",
    "--str", "--num", "--kw",
    "--radius-sm", "--radius-md", "--border-w",
    "--ease", "--transition-fast",
    "--shadow-tag", "--shadow-modal",
]

READ = """([style, theme, tokens]) => {
  __winnow.applyStyle(style);
  __winnow.applyThemeMode(theme);
  const cs = getComputedStyle(document.documentElement);
  const out = {};
  for (const t of tokens) out[t] = cs.getPropertyValue(t).trim();
  return out;
}"""


@pytest.fixture
def styles(page):
    """Function-scoped, like `page` itself — a module-scoped fixture may
    not take a per-test one."""
    return page.evaluate("() => Object.keys(__winnow.STYLES)")


@pytest.fixture(autouse=True)
def _restore(page):
    yield
    # Leave the shared context on the default, whatever this test set.
    page.evaluate("() => { __winnow.applyStyle('harvest'); __winnow.applyThemeMode('dark'); }")


def test_the_picker_offers_more_than_one_style(page, styles):
    assert len(styles) >= 5, styles


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_every_style_defines_every_token(page, styles, theme):
    missing = {}
    for style in styles:
        got = page.evaluate(READ, [style, theme, TOKENS])
        blank = [t for t, v in got.items() if not v]
        if blank:
            missing[style] = blank
    assert not missing, f"{theme}: tokens resolving to nothing — {missing}"


def test_the_two_themes_actually_differ(page, styles):
    """A skin whose light half was copied and not edited reads as a bug
    report about the theme toggle."""
    same = []
    for style in styles:
        dark = page.evaluate(READ, [style, "dark", ["--ink", "--text", "--panel"]])
        light = page.evaluate(READ, [style, "light", ["--ink", "--text", "--panel"]])
        if dark == light:
            same.append(style)
    assert not same, f"these styles have identical dark and light surfaces: {same}"


def test_light_is_lighter_than_dark(page, styles):
    """Catches a dark/light block written the wrong way round — which is
    easy to do on a skin whose dark half is the primary one."""
    def luminance(value):
        """A custom property comes back as its author value, so these are
        hex rather than the rgb() a resolved colour would be."""
        v = value.strip().lstrip("#")
        if len(v) == 3:
            v = "".join(c * 2 for c in v)
        return sum(int(v[i:i + 2], 16) for i in (0, 2, 4)) / 3

    wrong = []
    for style in styles:
        d = page.evaluate(READ, [style, "dark", ["--ink"]])["--ink"]
        li = page.evaluate(READ, [style, "light", ["--ink"]])["--ink"]
        if luminance(li) <= luminance(d):
            wrong.append((style, d, li))
    assert not wrong, f"light surface is not lighter than dark: {wrong}"


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_the_grid_stays_monospace_in_every_skin(page, styles, theme):
    """Winnow's grid is monospace so timestamps, hashes and paths line up
    down a column. A skin may restyle --ui freely (phosphor makes the
    chrome monospace, Jenna's theme makes it Segoe UI); none of them may
    take the grid out of --mono."""
    for style in styles:
        page.evaluate(READ, [style, theme, []])
        fam = page.evaluate(
            "() => getComputedStyle(document.querySelector('#body .cell')).fontFamily")
        assert "mono" in fam.lower() or "consolas" in fam.lower() or "menlo" in fam.lower(), \
            f"{style}/{theme}: grid cells are {fam}"

