"""Jenna's grid is drawn like the SQL pane's result table.

Asked for as "make the tables in Jenna's theme look more like the SQL
output tables". The SQL table reads as ruled because of three things —
the rule colour, a closed box, and a mixed-case mono header — and the
grid had none of them: its cells were ruled in --border-hairline, which
on Jenna's #1e1e1e is all but invisible, and its header shouted the
column names in letter-spaced uppercase UI type.

These read what the browser paints rather than what the stylesheet says,
because a rule that loses the cascade looks exactly like one never
written. The last test holds the change to Jenna: .row/.cell/.hcell are
shared by every skin.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

READ = """() => {
  const root = getComputedStyle(document.documentElement);
  const tok = (n) => {
    const p = document.createElement('div');
    p.style.color = `var(${n})`;
    document.body.append(p);
    const v = getComputedStyle(p).color;
    p.remove();
    return v;
  };
  const cs = (sel) => getComputedStyle(document.querySelector(sel));
  const hcell = cs('#app .hcell:not(.gutter-head)');
  const row = cs('#app .row');
  const cell = cs('#app .row .cell');
  const head = cs('#app .grid-head');
  const gutter = cs('#app .row .gutter');
  return {
    line: tok('--line'), panel2: tok('--panel-2'), hairline: tok('--border-hairline'),
    mono: root.getPropertyValue('--mono').trim(),
    hTransform: hcell.textTransform, hSpacing: hcell.letterSpacing, hFont: hcell.fontFamily,
    rowRule: row.borderBottomColor, cellRule: cell.borderRightColor,
    headBg: head.backgroundColor, headTop: head.borderTopWidth, headTopColor: head.borderTopColor,
    gutterLeft: gutter.borderLeftWidth, gutterLeftColor: gutter.borderLeftColor,
  };
}"""


def _read(page, style, theme="dark"):
    page.evaluate("([s, t]) => { __winnow.applyStyle(s); __winnow.applyThemeMode(t); }", [style, theme])
    page.wait_for_selector("#app .row .cell")
    return page.evaluate(READ)


@pytest.fixture(autouse=True)
def _restore(page):
    yield
    page.evaluate("() => { __winnow.applyStyle('harvest'); __winnow.applyThemeMode('dark'); }")


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_jenna_rules_cells_like_the_sql_table(page, theme):
    got = _read(page, "jenna", theme)
    assert got["rowRule"] == got["line"], got
    assert got["cellRule"] == got["line"], got


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_jenna_grid_is_a_closed_box(page, theme):
    got = _read(page, "jenna", theme)
    assert got["headTop"] == "1px" and got["headTopColor"] == got["line"], got
    assert got["gutterLeft"] == "1px" and got["gutterLeftColor"] == got["line"], got
    assert got["headBg"] == got["panel2"], got


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_jenna_header_is_mixed_case_mono(page, theme):
    got = _read(page, "jenna", theme)
    assert got["hTransform"] == "none", got
    assert got["hSpacing"] in ("normal", "0px"), got
    # The computed family is the --mono stack itself, not the UI face.
    first = got["mono"].split(",")[0].strip().strip('"')
    assert first in got["hFont"], got


def test_other_skins_keep_their_own_grid(page):
    """The grid rules are global; the Jenna treatment must not leak."""
    got = _read(page, "harvest", "dark")
    assert got["hTransform"] == "uppercase", got
    assert got["rowRule"] == got["hairline"], got
    assert got["gutterLeft"] == "0px", got
