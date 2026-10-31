"""The Pages site and the app's Harvest skin share their dark greys.

docs/site/assets/site.css copies Harvest's tokens and names the one each
value came from (`#161616;  /* dark --panel */`). Harvest dark used to be
green-cast while the site had the cast taken out, so the two drifted
apart and the app read as green next to its own website. Now they are the
same values, and this holds them there: change a grey in one place and
this fails until the other agrees.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SITE_CSS = ROOT / "docs" / "site" / "assets" / "site.css"
STYLE_CSS = ROOT / "static" / "style.css"

# `#1d1d1d;   /* dark --panel-2 */` — a value and the Harvest token it copies.
SITE_COPY = re.compile(r"(#[0-9a-fA-F]{6});\s*/\*\s*(dark|light)\s+(--[\w-]+)\s*\*/")


def _harvest(theme: str) -> dict[str, str]:
    css = STYLE_CSS.read_text(encoding="utf-8")
    m = re.search(r'html\[data-style="harvest"\]\[data-theme="%s"\]\s*\{(.*?)\n\}' % theme, css, re.S)
    assert m, f"no Harvest {theme} block in static/style.css"
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    return {k: v.lower() for k, v in re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", body)}


def _site_copies() -> list[tuple[str, str, str]]:
    if not SITE_CSS.exists():
        pytest.skip("docs/site is not in this tree")
    return [(v.lower(), theme, tok) for v, theme, tok in SITE_COPY.findall(SITE_CSS.read_text(encoding="utf-8"))]


def test_site_names_harvest_tokens_it_copies():
    # A rewording of site.css's comments must not quietly leave nothing
    # for the test below to check.
    themes = {theme for _, theme, _ in _site_copies()}
    assert themes == {"dark", "light"}, themes


def test_every_copied_value_matches_the_harvest_token():
    tokens = {"dark": _harvest("dark"), "light": _harvest("light")}
    wrong = [f"site.css {value} says {theme} {tok}, style.css has {tokens[theme].get(tok)}"
             for value, theme, tok in _site_copies() if tokens[theme].get(tok) != value]
    assert not wrong, wrong


def test_harvest_dark_greys_have_no_cast():
    """The green cast is what this replaced; a grey with any hue in it
    brings the mismatch back."""
    greys = ("--ink", "--panel", "--panel-2", "--panel-3", "--line", "--line-2", "--text",
             "--dim", "--row-even", "--row-hover", "--border-hairline", "--text-faint")
    dark = _harvest("dark")
    tinted = {t: dark[t] for t in greys if len({dark[t][1:3], dark[t][3:5], dark[t][5:7]}) != 1}
    assert not tinted, tinted
