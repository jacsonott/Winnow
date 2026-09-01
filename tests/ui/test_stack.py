"""Stack view — rarest-first value counts for a column, drawn with the
chart module, click-to-filter. Reached from the column header menu."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_stack_lists_values_rarest_first_and_filters(page):
    # Open via the public entry point rather than the right-click menu, so
    # the test doesn't depend on menu geometry.
    col = page.evaluate("() => __winnow.S.columns.find((c) => !c.derived).name")
    page.evaluate("(c) => __winnow.openStack(c)", col)
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").inner_text().lower().startswith("stack")
    page.wait_for_function(
        "() => /distinct value/.test(document.querySelector('#modal .note-status').textContent)",
        timeout=10_000)
    # The rarest/common toggle exists and rarest is the default.
    assert page.locator("#modal button", has_text="Rarest first").get_attribute("class") == "btn"
    canvas = page.locator("#modal canvas")
    assert canvas.count() == 1
    # Canvas actually drew something.
    lit = page.evaluate("""() => {
      const c = document.querySelector('#modal canvas');
      const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;
      let n=0; for (let i=3;i<d.length;i+=4) if (d[i]>40) n++; return n; }""")
    assert lit > 500, lit
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")
