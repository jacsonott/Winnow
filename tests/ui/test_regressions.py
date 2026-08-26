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


def test_a_many_term_advanced_search_collapses_instead_of_hogging_the_toolbar(page):
    """Each advanced term renders as a full editor row (connector / NOT /
    input / remove), so a saved filter carrying a long term list — the
    shipped tool sweep is 26 — used to stack the toolbar taller than the
    viewport and leave the grid zero visible rows. Past a handful of terms
    the bar must collapse to a one-line summary chip, expand on demand into
    an editor capped well short of the viewport, and minimize back."""
    page.evaluate("""() => {
      __winnow.S.searchMode = 'advanced';
      __winnow.S.searchTerms = Array.from({ length: 20 }, (_, i) =>
        ({ term: 'tool' + i, connector: 'OR', exclude: false }));
      __winnow.renderAdvancedChips();
      __winnow.syncSearchExpansion(true);
    }""")
    summary = page.locator(".adv-summary")
    assert "20 terms" in summary.inner_text()
    assert page.locator(".toolbar").bounding_box()["height"] < 120, "summary didn't keep the toolbar to one line"

    summary.click()  # expand: the full editor, but scroll-capped
    assert page.locator("#advancedSearchBar input").count() == 20
    assert page.locator(".toolbar").bounding_box()["height"] < page.viewport_size["height"] / 2, (
        "expanded editor may scroll, not shove the grid off-screen"
    )

    page.locator(".adv-summary").click()  # the trailing "▴ minimize"
    assert page.locator("#advancedSearchBar input").count() == 0
    assert page.locator(".toolbar").bounding_box()["height"] < 120

    # `/` with the list collapsed focuses the summary chip (there is no
    # input to land on) rather than throwing on input.select().
    page.keyboard.press("/")
    assert "adv-summary" in page.evaluate("() => document.activeElement.className")


def test_sorting_a_grouped_view_keeps_open_groups_open(page):
    """Sorting (or filtering, searching) rebuilds the view, and regroupAll
    rebuilt the group tree from scratch — every group the analyst had
    expanded snapped shut on each header click. The expanded set must
    survive any rebuild that keeps the same grouping columns."""
    page.evaluate("() => __winnow.addGroupLevel('EventId')")
    page.wait_for_function("() => __winnow.S.groups.length > 0")
    page.evaluate("() => __winnow.toggleGroup(1)")
    page.wait_for_function("() => __winnow.S.groups[1].expanded")
    opened = page.evaluate("() => __winnow.S.groups[1].value")

    page.evaluate("""() => {
      __winnow.S.sort = [{ column: 'Host', dir: 'asc' }];
      return __winnow.rebuildView({ keepScroll: false });
    }""")
    page.wait_for_function(
        "() => __winnow.S.groups.length > 0 && __winnow.S.groups.some((g) => g.expanded)"
    )
    still_open = page.evaluate(
        "() => __winnow.S.groups.filter((g) => g.expanded).map((g) => g.value)"
    )
    assert still_open == [opened], f"open group {opened!r} didn't survive the sort: {still_open}"
    # ...and it's really open: the tree contributes data rows, not just headers.
    assert page.evaluate("() => __winnow.S.groupTotalRows > __winnow.S.groups.length")

    # Changing the grouping COLUMNS is a different tree — nothing to restore.
    page.evaluate("() => __winnow.dropGrouping()")  # evaluate awaits the returned promise
    page.evaluate("() => __winnow.addGroupLevel('Host')")
    page.wait_for_function(
        "() => __winnow.S.groupByCols.length === 1 && __winnow.S.groupByCols[0] === 'Host' && __winnow.S.groups.length > 0"
    )
    assert page.evaluate("() => __winnow.S.groups.every((g) => !g.expanded)")


def test_month_name_timestamps_parse_client_side(page):
    """Same third family the server now recognizes, mirrored in
    parseTimestamp — display formatting and the client's time math must
    read the value the same way TS_NORMALIZE does."""
    got = page.evaluate("() => __winnow.parseTimestamp('JUN 23 2026 00:11:00')")
    assert got == {"y": 2026, "mo": 6, "d": 23, "h": 0, "mi": 11, "s": 0, "frac": ""}
    assert page.evaluate("() => __winnow.parseTimestamp('June 23, 2026 5:11 PM').h") == 17
    assert page.evaluate("() => __winnow.parseTimestamp('23 Jun 2026').d") == 23
    assert page.evaluate("() => __winnow.parseTimestamp('Monday 23 2026')") is None


def test_jump_to_timestamp_is_reachable_from_the_timeframe_dialog(page):
    page.evaluate("() => __winnow.openTimeRangeModal()")
    page.wait_for_selector("#modal:not([hidden])")
    page.click("#modalBody .btn:has-text('Jump to timestamp…')")
    page.wait_for_timeout(200)
    assert page.locator("#modalTitle").inner_text().lower() == "jump to timestamp"
    page.keyboard.press("Escape")


def test_saved_filters_modal_groups_by_header_set_and_stays_dense(page):
    """The flat list (one row per filter, nickname button on every row,
    ~52px pitch) drowned once the 28 shipped filters landed. Filters now
    sit under one header per column set — which is also the unit they
    cycle in — with the nickname control on the section, and rows dense
    enough to scan."""
    page.evaluate("() => __winnow.openSavedFiltersModal()")
    page.wait_for_selector(".sf-group-head")
    assert page.locator(".sf-group-head").count() >= 1
    rows = page.locator(".sf-row")
    assert rows.count() >= 2
    b1, b2 = rows.nth(0).bounding_box(), rows.nth(1).bounding_box()
    assert b2["y"] - b1["y"] <= 32, f"row pitch {b2['y'] - b1['y']}px — the list has bloated again"
    page.keyboard.press("Escape")


def test_settings_no_longer_duplicates_the_saved_filters_list(page):
    """Settings used to render every saved filter a second time. It keeps
    import/export and a pointer; the list lives in the Saved filters modal
    alone."""
    page.keyboard.press("?")
    page.wait_for_selector("#modal:not([hidden])")
    page.click(".settings-section-head:has-text('Saved filters')")
    sec = page.locator(".settings-section:has(.settings-section-head:has-text('Saved filters'))")
    assert sec.locator(".session-row, .sf-row").count() == 0
    sec.locator(".btn", has_text="Open saved filters").click()
    page.wait_for_selector(".sf-group-head")
    assert page.locator("#modalTitle").inner_text().lower() == "saved filters"
    page.keyboard.press("Escape")


def test_shortcuts_go_inert_while_a_dialog_is_open(page):
    """With the filter builder up, q/w kept cycling saved filters
    underneath it, digits kept tagging, v opened the value picker behind
    the card. A dialog owns the keyboard: everything but Escape stops."""
    page.keyboard.press("e")
    page.wait_for_selector("#modal:not([hidden])")
    page.keyboard.press("v")
    page.wait_for_timeout(150)
    assert page.locator(".menu").count() == 0, "the value picker opened behind the dialog"
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.keyboard.press("v")
    page.wait_for_timeout(150)
    assert page.locator(".menu").count() == 1, "v must still work once the dialog is gone"
    page.keyboard.press("Escape")


def test_is_any_of_renders_one_value_per_line(page):
    """'is any of (one per line)' rendered in a single-line input, so the
    newlines collapsed and ['6006','6008','41','1074'] displayed as the
    run-together 60066008411074. List-valued conditions get a textarea."""
    page.evaluate("""() => {
      __winnow.S.filterTree = { type: 'group', op: 'AND', children: [
        { type: 'cond', column: 'EventId', op: 'in', value: ['6006', '6008', '41', '1074'] }] };
    }""")
    page.keyboard.press("e")
    page.wait_for_selector("#modal:not([hidden])")
    box = page.locator(".fb-cond textarea")
    assert box.count() == 1
    assert box.input_value() == "6006\n6008\n41\n1074"
    page.keyboard.press("Escape")
    page.evaluate("() => { __winnow.S.filterTree = { type: 'group', op: 'AND', children: [] }; }")


def test_saved_filters_are_reachable_from_the_builder(page):
    page.keyboard.press("e")
    page.wait_for_selector("#modal:not([hidden])")
    page.click("#modalBody .btn:has-text('Saved filters…')")
    page.wait_for_timeout(200)
    assert page.locator("#modalTitle").inner_text().lower() == "saved filters"  # CSS uppercases it
    page.keyboard.press("Escape")


def test_a_cond_root_saved_filter_really_applies_and_renders(page):
    """Version 1 of the shipped filters stored several single-condition
    trees with the condition AS the root. The server compiles that shape,
    but the client's spec gate read it as "no filter" — so the ★ button
    said applied while the grid showed every row, and the builder opened
    empty with the SQL box full. Every boundary now normalizes the root
    to a group; this drives the old payload shape end to end."""
    total = page.evaluate("() => __winnow.S.view.row_count")
    page.evaluate("""() => __winnow.applyPreset({ id: 9901, name: 'condroot', col_names: [],
      payload: { filter_tree: { type: 'cond', column: 'EventId', op: 'in', value: ['4624'] },
                 search: '', search_mode: 'contains', search_terms: [] } })""")
    page.wait_for_function(f"() => __winnow.S.view.row_count < {total}")
    assert page.evaluate("() => __winnow.S.view.row_count") == 50  # 200 rows, EventId cycles 4 values
    assert page.evaluate("() => __winnow.hasActiveFilterTree()")

    page.keyboard.press("e")  # the filter-builder binding
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator(".fb-cond").count() == 1, "the builder must show the condition, not an empty tree"
    page.keyboard.press("Escape")
