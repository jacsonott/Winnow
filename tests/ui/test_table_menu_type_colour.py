"""The table menu writes each column's value type in a colour of its own.

The type was a plain span inheriting the label's colour, so a row read
"RecordNumber number" in one colour, the type looking like the second
word of the name. Checked in every skin under both themes: each skin
points --col-type at a colour of its own choosing, and one that left it
out, or picked its own --text, would bring the bug straight back.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

READ = """([style, theme]) => {
  __winnow.applyStyle(style);
  __winnow.applyThemeMode(theme);
  const row = document.querySelector('#modalBody .collist-row');
  const name = row.querySelector('label > span:not(.collist-type)');
  const type = row.querySelector('label > .collist-type');
  return {
    name: name.textContent, type: type.textContent.trim(),
    nameColour: getComputedStyle(name).color, typeColour: getComputedStyle(type).color,
  };
}"""


@pytest.fixture(autouse=True)
def _restore(page):
    yield
    # Leave the shared context on the default, whatever this test set.
    page.evaluate("() => { __winnow.applyStyle('harvest'); __winnow.applyThemeMode('dark'); }")


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_the_type_is_not_the_colour_of_the_name_in_any_skin(page, theme):
    page.evaluate("() => __winnow.openTableMenu()")
    page.wait_for_selector("#modalBody .collist-row .collist-type")
    styles = page.evaluate("() => Object.keys(__winnow.STYLES)")
    same = {}
    for style in styles:
        got = page.evaluate(READ, [style, theme])
        assert got["type"], f"{style}: the first row shows no type — {got}"
        if got["nameColour"] == got["typeColour"]:
            same[style] = got
    assert not same, f"{theme}: name and type share a colour — {same}"
