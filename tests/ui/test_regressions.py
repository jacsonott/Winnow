"""Regressions that a green backend suite let through.

One test per bug that actually reached the analyst, each asserting the thing
that was wrong rather than the fix that made it right — so they'd fail again
if the fix were reverted, refactored away, or re-broken from a different
direction.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# Reads every column's header cell and the filter cell under it. Returns the
# rendered widths plus what the header's label needs vs. what it got, which
# is how "the header name is truncated" becomes an assertion.
MEASURE = """() => {
  const out = [];
  document.querySelectorAll('.hcell[data-col]').forEach((h) => {
    const col = h.dataset.col;
    const input = document.querySelector(`.fcell input[data-col="${CSS.escape(col)}"]`);
    const label = h.querySelector('.label');
    out.push({
      col,
      head: Math.round(h.getBoundingClientRect().width),
      filter: input ? Math.round(input.parentElement.getBoundingClientRect().width) : null,
      labelNeeds: label ? label.scrollWidth : null,
      labelHas: label ? label.clientWidth : null,
    });
  });
  return out;
}"""


def test_filter_cells_stay_the_width_of_their_columns(page):
    """The filter row is pinned to the header row by giving both the same
    flex-basis — which stops being true the moment a filter cell's *content*
    can push it wider. `.fcell` becoming a flex container (to seat the value
    picker's button) did exactly that: a flex item's automatic minimum size is
    content-based, and a text input's intrinsic width is ~177px, so every
    column narrower than that had a filter box wider than its header and the
    two rows drifted apart across the grid.
    """
    for col in page.evaluate(MEASURE):
        assert col["filter"] == col["head"], (
            f"{col['col']}: filter cell {col['filter']}px under a {col['head']}px header — "
            "the filter row is no longer pinned to the columns"
        )


def test_filter_cells_track_a_resized_column(page):
    """Same pairing, after a width actually changes: the picker button must
    give up space rather than hold the cell open."""
    page.evaluate("""() => {
      __winnow.S.layout['Host'] = { ...(__winnow.S.layout['Host'] || {}), w: 70 };
      __winnow.renderHead();
    }""")
    host = next(c for c in page.evaluate(MEASURE) if c["col"] == "Host")
    assert host["head"] == 70 and host["filter"] == 70, host


def test_autofit_leaves_room_for_the_header_name(page):
    """Autofit used to size a column from `max(dataChars, name.length) * 7 + 24`,
    which accounts for none of the header's chrome (sort arrow, the derived ƒ
    mark, 8px of padding either side) and isn't 7px per character for an
    uppercase letter-spaced font either. A column of one-character values
    under a long header came out with the header ellipsised — the column was
    fitted to its content and became unidentifiable.
    """
    page.click("#body")
    page.keyboard.press("=")
    page.wait_for_function("() => !document.querySelector('.busy-bar:not([hidden])')")
    page.wait_for_timeout(400)
    for col in page.evaluate(MEASURE):
        assert col["labelHas"] >= col["labelNeeds"], (
            f"{col['col']}: header needs {col['labelNeeds']}px, got {col['labelHas']}px — "
            "autofit truncated the column's own name"
        )


def test_autofit_respects_the_configured_cap(page):
    """The cap is a user setting now, so it has to be *read* at fit time
    rather than baked in. CommandLine's values are ~350 characters: uncapped
    it fits to thousands of pixels, which is the whole reason a default cap
    exists (rows are `width: max-content`, so one column decides how far every
    other column has to be scrolled)."""
    def fit_with(cap):
        page.evaluate(f"() => {{ __winnow.S.appearance.autofitMax = {cap}; __winnow.saveAppearance(); }}")
        page.click("#body")
        page.keyboard.press("=")
        page.wait_for_timeout(500)
        return next(c for c in page.evaluate(MEASURE) if c["col"] == "CommandLine")["head"]

    assert fit_with(900) == 900
    assert fit_with(400) == 400
    assert fit_with(0) > 1500, "0 means uncapped — a long column should be allowed to be long"


def test_autofit_shrinks_an_over_wide_column_back(page):
    """Autofit has to be able to make a column *smaller*, which is the half
    that's easy to lose: the header's requirement was originally derived from
    `hcell.clientWidth - label.clientWidth`, which is chrome only while the
    cell is exactly as wide as its contents. On a widened column that
    difference is mostly slack, so the header claimed to need roughly the
    current width and every fit was a no-op that looked stable."""
    page.evaluate("""() => {
      __winnow.S.layout['Host'] = { ...(__winnow.S.layout['Host'] || {}), w: 600 };
      __winnow.renderHead(); __winnow.render();
    }""")
    assert next(c for c in page.evaluate(MEASURE) if c["col"] == "Host")["head"] == 600
    page.click("#body")
    page.keyboard.press("=")
    page.wait_for_timeout(500)
    host = next(c for c in page.evaluate(MEASURE) if c["col"] == "Host")
    assert host["head"] < 200, f"autofit left a 600px column at {host['head']}px — it can only grow"
    assert host["labelHas"] >= host["labelNeeds"]


def test_resetting_the_keymap_actually_resets_it(page):
    """"Reset to defaults" silently did nothing on a profile with no stored
    keymap. The settings UI mutates each action's key array in place, and a
    shallow `{...DEFAULT_KEYMAP}` handed it DEFAULT_KEYMAP's own arrays — so
    binding a key edited the defaults themselves, and resetting copied the
    now-polluted defaults straight back."""
    page.keyboard.press("?")
    page.wait_for_selector("#modal:not([hidden])")
    page.click(".settings-section-head:has-text('Keyboard shortcuts')")
    row = page.locator(".settings-key-row").first
    before = row.inner_text()

    row.locator(".btn", has_text="+ key").click()
    page.keyboard.press("y")
    page.wait_for_timeout(200)
    assert "y" in row.inner_text(), "the new binding never landed, so this proves nothing"

    page.click("#modalBody .btn:has-text('Reset to defaults')")
    page.wait_for_timeout(200)
    assert row.inner_text() == before, "reset did not restore the default bindings"
    # And the module-level defaults themselves must be unpolluted, which is
    # the actual bug — a reset that only fixes S.keymap would still pass above
    # on the first run and fail after a reload.
    assert page.evaluate("() => __winnow.DEFAULT_KEYMAP.moveDown.join(',')") == "ArrowDown,j"
