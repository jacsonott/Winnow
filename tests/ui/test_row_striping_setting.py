"""Row striping is a preference, saved per skin.

Per skin because the skins disagree about it by construction: Jenna's
theme reconstructs a grid that has no zebra, the rest are drawn around
having one. A single global switch would be wrong for half the list the
moment it was set.

Everything here reads the backgrounds the browser actually paints rather
than the --row-even token. The "off" rule has to outweigh six skin blocks
that set that token at [data-style][data-theme], and a custom property
that loses a cascade looks exactly like one that was never set -- so the
only honest question is what colour the rows came out.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# Distinct backgrounds across a run of rows that carry no other state.
# One colour means flat; two means striped.
BGS = """() => {
  const rows = [...document.querySelectorAll('#body .row')]
    .filter((r) => !/selected|diff-/.test(r.className))
    .slice(0, 12);
  return { n: rows.length,
           bgs: [...new Set(rows.map((r) => getComputedStyle(r).backgroundColor))] };
}"""


def shades(page):
    page.mouse.move(0, 0)   # :hover would read as another background
    got = page.evaluate(BGS)
    assert got["n"] >= 4, f"not enough plain rows to judge striping: {got}"
    return len(got["bgs"])


def use(page, style, stripes=None):
    page.evaluate("(s) => __winnow.applyStyle(s)", style)
    if stripes is not None:
        page.evaluate("(on) => __winnow.setStripes(on)", stripes)


@pytest.fixture(autouse=True)
def _restore(page):
    yield
    page.evaluate("""() => {
      __winnow.S.appearance.stripes = {};
      __winnow.applyStyle('harvest');
      __winnow.applyThemeMode('dark');
      __winnow.paintStripes();
      __winnow.saveAppearance();
    }""")


# ---------------------------------------------------------- the defaults

def test_jenna_starts_flat_and_the_others_striped(page):
    """The shipped defaults, which are the whole reason this is per skin."""
    use(page, "jenna")
    assert shades(page) == 1, "Jenna's theme should default to no striping"
    for style in ["harvest", "panel", "phosphor", "blueprint", "studio"]:
        use(page, style)
        assert shades(page) == 2, f"{style} should default to striped rows"


# ------------------------------------------------------------ the toggle

def test_turning_it_on_stripes_a_flat_skin(page):
    use(page, "jenna")
    assert shades(page) == 1
    page.evaluate("() => __winnow.setStripes(true)")
    assert shades(page) == 2, "striping turned on for Jenna did not reach the rows"


def test_turning_it_off_flattens_a_striped_skin(page):
    use(page, "harvest")
    assert shades(page) == 2
    page.evaluate("() => __winnow.setStripes(false)")
    assert shades(page) == 1, "striping turned off for Harvest did not reach the rows"


@pytest.mark.parametrize("style", ["harvest", "panel", "phosphor", "blueprint", "studio", "jenna"])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_off_is_off_in_every_skin_and_both_themes(page, style, theme):
    """The off rule collapses --row-even onto --ink. --ink is per skin AND
    per theme, so this is the combination most likely to have a hole."""
    use(page, style, stripes=False)
    page.evaluate("(t) => __winnow.applyThemeMode(t)", theme)
    assert shades(page) == 1, f"{style}/{theme} still stripes with striping off"


# -------------------------------------------------------- per skin, not global

def test_the_choice_is_remembered_per_skin(page):
    """The point of the design: setting it on one skin must not reach the
    others, and coming back to a skin must find the answer left there."""
    use(page, "jenna", stripes=True)      # against Jenna's own default
    use(page, "harvest")
    assert shades(page) == 2, "Harvest should still be on its own default"
    page.evaluate("() => __winnow.setStripes(false)")   # against Harvest's
    use(page, "jenna")
    assert shades(page) == 2, "Jenna did not keep the override set on it"
    use(page, "harvest")
    assert shades(page) == 1, "Harvest did not keep the override set on it"


def test_only_the_overridden_skins_are_stored(page):
    """Absent means 'use the skin's default', which is what lets a shipped
    default be changed later without overwriting somebody's choice."""
    use(page, "jenna", stripes=True)
    saved = page.evaluate(
        "() => JSON.parse(localStorage.getItem('winnow.appearance') || '{}').stripes")
    assert saved == {"jenna": True}, saved


def test_an_install_with_no_setting_gets_the_skin_defaults(page):
    """What every existing install looks like on upgrade: no key at all."""
    got = page.evaluate("""() => {
      const keep = __winnow.S.appearance.stripes;
      __winnow.S.appearance.stripes = undefined;
      const out = { jenna: __winnow.stripesOn('jenna'), harvest: __winnow.stripesOn('harvest') };
      __winnow.S.appearance.stripes = keep;
      return out;
    }""")
    assert got == {"jenna": False, "harvest": True}, got


# ----------------------------------------------------------------- the UI

def test_the_settings_checkbox_drives_it(page):
    use(page, "jenna")
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    page.click("#modal .settings-section-head:has-text('Appearance')")
    box = page.locator("#appearanceStripes")
    box.wait_for(state="visible")
    assert box.is_checked() is False, "the box should show Jenna's default"
    # The note names the skin the box is about, or a per-skin setting reads
    # as one that did not save.
    assert "Jenna" in page.locator("#appearanceStripes").locator("xpath=../..").inner_text()
    box.check()
    page.keyboard.press("Escape")
    assert shades(page) == 2, "the checkbox did not reach the rows"
