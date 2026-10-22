"""The histogram's Value and Groups views — the bars colored by a value
rather than by tag.

Value: the rows holding one value, as if they were tagged. The value is
chosen with "Show in histogram" on a cell's right-click menu and stays put
while the analyst clicks around, unless "Follow the selected cell" (a
right-click on the Value button) is on, when it is the selected cell's.
Groups: while the grid is grouped, its first N groups get a palette color
each and the same color beside their headings, and a color stays with its
value when a filter reorders the groups — the color an analyst has learned
for one group must not move to another one.

Both are views of their own beside Tags, one pressed at a time. The
fixture case has 200 rows: EventId cycles 4624/4625/4688/1 (50 each) and
Host H0–H4 (40 each). Grouping, filters and the strip itself are view
state, put back in a finally; nothing here writes to the case.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

ROWS = 200


def _open_strip(page):
    page.evaluate("() => { if (!__winnow.histogramOpen()) __winnow.toggleHistogram(true); }")
    _strip_up(page)


def _strip_up(page):
    page.wait_for_function("() => !document.getElementById('histogramPanel').hidden")
    page.wait_for_function(
        "(n) => (document.querySelector('.th-info')||{}).textContent.startsWith(n)",
        arg=str(ROWS), timeout=15_000)


def _close_strip(page):
    # Closed and forgotten, as the other histogram modules leave it: a
    # strip left open keeps redrawing under every later test in the shard.
    page.evaluate("""() => { __winnow.setHistogramView('plain'); __winnow.toggleHistogram(false);
                             localStorage.removeItem('winnow.histogram'); }""")


def _legend(page):
    return page.evaluate(
        "() => [...document.querySelectorAll('.th-legend-item')].map((n) => n.textContent)")


def _wait_legend(page, text):
    page.wait_for_function(
        """(t) => !document.getElementById('histogramPanel').classList.contains('is-outdated')
             && [...document.querySelectorAll('.th-legend-item')].some((n) => n.textContent === t)""",
        arg=text, timeout=15_000)


def _cell(page, row, col_name):
    ci = page.evaluate("() => __winnow.visibleCols()").index(col_name)
    return page.locator("#body .row").nth(row).locator(".cell").nth(ci)


def _click_cell(page, row, col_name):
    _cell(page, row, col_name).click()


def _show_in_histogram(page, row, col_name):
    _cell(page, row, col_name).click(button="right")
    page.locator(".menu:not(.menu-sub) .menu-item", has_text="Show in histogram").click()


def _pressed(page):
    return page.evaluate("""() => [...document.querySelectorAll('#histogramPanel .th-views .btn')]
        .filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.textContent)""")


def _stays_put(page, row, col_name):
    """Select a cell and check the Value view did not move: a render after
    the focus lands is where a following view would ask (followSplit), and
    asking fades the strip at once."""
    _click_cell(page, row, col_name)
    page.wait_for_function("(c) => __winnow.S.cellFocus && __winnow.S.cellFocus.name === c", arg=col_name)
    page.evaluate("() => __winnow.render()")
    assert not page.evaluate("() => document.getElementById('histogramPanel').classList.contains('is-outdated')")


def test_value_is_chosen_from_the_cell_menu_and_stays_put(page):
    try:
        _open_strip(page)
        page.locator("#histogramPanel .th-view-value").click()
        # Nothing chosen yet: the view says how to choose, rather than
        # drawing plain bars under a pressed button.
        page.wait_for_selector("#histogramPanel .th-legend-hint:has-text('Show in histogram')")
        _show_in_histogram(page, 0, "Host")
        _wait_legend(page, "Host = H0 · 40 rows")
        assert _legend(page) == ["Everything else", "Host = H0 · 40 rows"]
        got = page.evaluate("""() => {
          const d = __winnow.histogramData();
          const flat = new Map(d.buckets);
          return { kind: d.stack.kind, values: d.stack.values,
                   bars: d.stack.buckets.map(([b, c]) => [c.reduce((a, n) => a + n, 0), flat.get(b)]) };
        }""")
        assert got["kind"] == "value" and got["values"] == ["H0"]
        # One row, one segment: the split bars are the plain bars.
        assert got["bars"] and all(seg == whole for seg, whole in got["bars"]), got["bars"]
        # Selecting another cell does not change what is charted.
        _stays_put(page, 2, "EventId")
        assert _legend(page) == ["Everything else", "Host = H0 · 40 rows"]
        assert _pressed(page) == ["Value"]
    finally:
        _close_strip(page)


def test_show_in_histogram_opens_the_strip(page):
    try:
        page.evaluate("() => __winnow.toggleHistogram(false)")
        page.wait_for_function("() => document.getElementById('histogramPanel').hidden")
        _show_in_histogram(page, 1, "EventId")
        _strip_up(page)
        _wait_legend(page, "EventId = 4625 · 50 rows")
        assert _pressed(page) == ["Value"]
    finally:
        _close_strip(page)


def test_right_click_on_value_follows_the_selected_cell(page):
    try:
        _open_strip(page)
        _click_cell(page, 0, "Host")
        page.locator("#histogramPanel .th-view-value").click(button="right")
        item = page.locator(".menu:not(.menu-sub) .menu-item", has_text="Follow the selected cell")
        assert item.locator(".menu-check").inner_text() == ""
        item.click()
        # On, it takes the cell already selected and turns the view on.
        _wait_legend(page, "Host = H0 · 40 rows")
        assert _pressed(page) == ["Value"]
        # Another cell, another column: the chart follows it.
        _click_cell(page, 2, "EventId")
        _wait_legend(page, "EventId = 4688 · 50 rows")
        # And off again from the same place, the check showing which it is.
        page.locator("#histogramPanel .th-view-value").click(button="right")
        item = page.locator(".menu:not(.menu-sub) .menu-item", has_text="Follow the selected cell")
        assert item.locator(".menu-check").inner_text() == "✓"
        item.click()
        _stays_put(page, 0, "Host")
        assert _legend(page) == ["Everything else", "EventId = 4688 · 50 rows"]
    finally:
        _close_strip(page)


def test_one_view_at_a_time(page):
    try:
        _open_strip(page)
        page.locator("#histogramPanel .th-stack").click()
        assert _pressed(page) == ["Tags"]
        page.locator("#histogramPanel .th-view-groups").click()
        assert _pressed(page) == ["Groups"]
        # Pressing the pressed one goes back to the plain chart.
        page.locator("#histogramPanel .th-view-groups").click()
        assert _pressed(page) == []
        assert page.evaluate("() => __winnow.histogramView()") == "plain"
    finally:
        _close_strip(page)


def _heading_colors(page):
    """Each top-level group heading's value and the color of its swatch
    (None without one), as computed colors so they compare with the
    legend's."""
    return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll(
        '.group-header-row[data-level="0"]')].map((r) => {
          const sw = r.querySelector('.swatch');
          return [r.querySelector('.group-header-value').textContent,
                  sw ? getComputedStyle(sw).backgroundColor : null];
        }))""")


def _legend_colors(page):
    return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.th-legend-item')]
        .map((n) => [n.textContent, getComputedStyle(n.querySelector('.swatch')).backgroundColor]))""")


def test_groups_color_the_first_groups_and_their_headings(page):
    try:
        _open_strip(page)
        page.locator("#histogramPanel .th-view-groups").click()
        page.wait_for_selector("#histogramPanel .th-legend-hint:has-text('Group by a column')")
        page.evaluate("() => __winnow.addGroupLevel('Host')")
        page.wait_for_function("() => __winnow.S.groups.length === 5")
        page.select_option("#histogramPanel .th-top", "3")
        first = page.evaluate("() => __winnow.S.groups.slice(0, 3).map((g) => g.value)")
        _wait_legend(page, first[2])
        assert _legend(page) == ["Other", *first]
        legend = _legend_colors(page)
        heads = _heading_colors(page)
        # The first three wear their segment's color; the other two none.
        for v in first:
            assert heads[v] == legend[v], (v, heads, legend)
        assert len(set(legend[v] for v in first)) == 3, "two groups share a color"
        assert sum(1 for c in heads.values() if c is None) == 2

        # Filter the first group out. The others move up a place; their
        # colors must not move with the rank.
        with_color = {v: legend[v] for v in first[1:]}
        page.evaluate("(v) => __winnow.filterByValue('Host', v, { exclude: true })", first[0])
        page.wait_for_function("(v) => !__winnow.S.groups.some((g) => g.value === v)", arg=first[0])
        page.wait_for_function(
            """(v) => { const d = __winnow.histogramData();
                 return d && d.stack && d.stack.kind === 'groups' && !d.stack.values.includes(v)
                   && !document.getElementById('histogramPanel').classList.contains('is-outdated'); }""",
            arg=first[0], timeout=15_000)
        legend = _legend_colors(page)
        heads = _heading_colors(page)
        for v, color in with_color.items():
            assert legend[v] == color and heads[v] == color, (v, color, legend, heads)
    finally:
        _close_strip(page)
        page.evaluate("async () => { await __winnow.dropGrouping(); await __winnow.clearAllFilters(); }")
