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


# The scroll box around the canvas — what used to be height:min(60vh,520px)
# no matter how many bars went in it.
BOX = "() => { const c = document.querySelector('#modal canvas'); return { box: c.parentElement.getBoundingClientRect().height, canvas: c.getBoundingClientRect().height, cap: Math.min(window.innerHeight * 0.6, 520) }; }"


def _open_stack(page, column):
    page.evaluate("(c) => __winnow.openStack(c)", column)
    page.wait_for_selector("#modal:not([hidden])")
    page.wait_for_function(
        "() => /distinct value/.test(document.querySelector('#modal .note-status').textContent)",
        timeout=10_000)


def test_a_stack_of_a_few_values_is_a_box_a_few_bars_tall(page):
    """Nine values opened the same screen-high box as nine hundred: the
    height was fixed, so the bars sat in the top corner of four fifths of
    nothing. Host stacks to five values in the fixture, and five bars at
    22px each are nowhere near the cap."""
    _open_stack(page, "Host")
    try:
        m = page.evaluate(BOX)
        assert m["box"] < m["cap"] / 2, m       # the symptom: box == cap, whatever the count
        assert abs(m["box"] - m["canvas"]) <= 4, m   # and it is exactly its content, plus borders
    finally:
        page.keyboard.press("Escape")
        page.wait_for_selector("#modal[hidden]", state="attached")


def test_a_stack_of_many_values_still_stops_at_the_cap(page):
    """The other half of the same rule: sizing to content must not let a
    long tail push the modal off the screen. Timestamp is unique per row in
    the fixture, so this is the capped-and-scrolling case."""
    _open_stack(page, "Timestamp")
    try:
        m = page.evaluate(BOX)
        assert abs(m["box"] - m["cap"]) <= 2, m
        assert m["canvas"] > m["box"], m        # so the rest is reachable by scrolling
    finally:
        page.keyboard.press("Escape")
        page.wait_for_selector("#modal[hidden]", state="attached")
