"""The grid's rows must not sit in their own compositing layer.

Chrome refuses subpixel (LCD) text antialiasing inside a composited
layer, so promoting the rows container silently drops every glyph in the
grid to grayscale AA. Against the rest of the app -- and against the
native Windows grids Winnow sits beside on an analyst's screen -- that
reads as the grid's text being bolder and blurrier than everything
around it. It is pure loss: render() rewrites the rows' contents on
every scroll step, so the promotion unlocks no fast path, and an A/B/A/B
scroll measurement found no frame-time difference either way.

`will-change: transform` on .rows is the specific reflex that caused it,
and is the thing most likely to come back: it is what you reach for on a
transform-scrolled virtual list, and nothing about the result looks
broken -- the text just quietly gets worse.

This asserts the absence of the promotion rather than measuring the
antialiasing, because LCD text is off in headless Chrome unless the
browser is launched with --enable-lcd-text, which the shared context
here is not. The measurement behind it is recorded in style.css.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# Each promotes to its own layer, and each contains nothing but text.
SCROLLERS = [".rows", ".timeline-rows"]


@pytest.mark.parametrize("selector", SCROLLERS)
def test_the_row_container_is_not_promoted_to_a_layer(page, selector):
    got = page.evaluate(
        """(sel) => {
          const el = document.querySelector(sel);
          if (!el) return null;
          const cs = getComputedStyle(el);
          return { willChange: cs.willChange, backface: cs.backfaceVisibility,
                   perspective: cs.perspective, filter: cs.filter };
        }""",
        selector,
    )
    if got is None:
        pytest.skip(f"{selector} is not in the DOM in this case")
    assert got["willChange"] == "auto", (
        f"{selector} declares will-change: {got['willChange']!r} — this costs the "
        f"grid subpixel antialiasing; see the note in style.css")
