"""The histogram's Value and Groups views — the bars coloured by a value
rather than by tag.

Value: the rows holding the selected cell's value, as if they were
tagged. It follows the cell focus, so clicking another cell is how you
chart another value. Groups: while the grid is grouped, its first N
groups get a palette colour each and the same colour beside their
headings, and a colour stays with its value when a filter reorders the
groups — the colour an analyst has learnt for one group must not move to
another one.

Both are views of their own beside Tags, one pressed at a time. The
fixture case has 200 rows: EventId cycles 4624/4625/4688/1 (50 each) and
Host H0–H4 (40 each). Grouping and filters are view state, put back in a
finally; nothing here writes to the case.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

ROWS = 200


def _open_strip(page):
    page.evaluate("() => { if (!__winnow.histogramOpen()) __winnow.toggleHistogram(true); }")
    page.wait_for_function("() => !document.getElementById('histogramPanel').hidden")
    page.wait_for_function(
        "(n) => (document.querySelector('.th-info')||{}).textContent.startsWith(n)",
        arg=str(ROWS), timeout=15_000)


def _legend(page):
    return page.evaluate(
        "() => [...document.querySelectorAll('.th-legend-item')].map((n) => n.textContent)")


def _wait_legend(page, text):
    page.wait_for_function(
        """(t) => !document.getElementById('histogramPanel').classList.contains('is-outdated')
             && [...document.querySelectorAll('.th-legend-item')].some((n) => n.textContent === t)""",
        arg=text, timeout=15_000)


def _click_cell(page, row, col_name):
    ci = page.evaluate("() => __winnow.visibleCols()").index(col_name)
    page.locator("#body .row").nth(row).locator(".cell").nth(ci).click()


def _pressed(page):
    return page.evaluate("""() => [...document.querySelectorAll('#histogramPanel .th-views .btn')]
        .filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.textContent)""")


def test_value_follows_the_selected_cell(page):
    try:
        _open_strip(page)
        page.locator("#histogramPanel .th-view-value").click()
        # Nothing selected yet: the view says what it is waiting for
        # rather than drawing plain bars under a pressed button.
        page.wait_for_selector("#histogramPanel .th-legend-hint:has-text('Select a cell')")
        _click_cell(page, 0, "Host")
        _wait_legend(page, "Host = H0 · 40 rows")
        assert _legend(page) == ["Everything else", "Host = H0 · 40 rows"]
        # Another cell, another column: the chart follows it.
        _click_cell(page, 2, "EventId")
        _wait_legend(page, "EventId = 4688 · 50 rows")
        got = page.evaluate("""() => {
          const d = __winnow.histogramData();
          const flat = new Map(d.buckets);
          return { kind: d.stack.kind, values: d.stack.values,
                   bars: d.stack.buckets.map(([b, c]) => [c.reduce((a, n) => a + n, 0), flat.get(b)]) };
        }""")
        assert got["kind"] == "value" and got["values"] == ["4688"]
        # One row, one segment: the split bars are the plain bars.
        assert got["bars"] and all(seg == whole for seg, whole in got["bars"]), got["bars"]
        assert _pressed(page) == ["Value"]
    finally:
        page.evaluate("() => __winnow.setHistogramView('plain')")


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
        page.evaluate("() => __winnow.setHistogramView('plain')")


def _heading_colours(page):
    """Each top-level group heading's value and the colour of its swatch
    (None without one), as computed colours so they compare with the
    legend's."""
    return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll(
        '.group-header-row[data-level="0"]')].map((r) => {
          const sw = r.querySelector('.swatch');
          return [r.querySelector('.group-header-value').textContent,
                  sw ? getComputedStyle(sw).backgroundColor : null];
        }))""")


def _legend_colours(page):
    return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.th-legend-item')]
        .map((n) => [n.textContent, getComputedStyle(n.querySelector('.swatch')).backgroundColor]))""")


def test_groups_colour_the_first_groups_and_their_headings(page):
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
        legend = _legend_colours(page)
        heads = _heading_colours(page)
        # The first three wear their segment's colour; the other two none.
        for v in first:
            assert heads[v] == legend[v], (v, heads, legend)
        assert len(set(legend[v] for v in first)) == 3, "two groups share a colour"
        assert sum(1 for c in heads.values() if c is None) == 2

        # Filter the first group out. The others move up a place; their
        # colours must not move with the rank.
        with_colour = {v: legend[v] for v in first[1:]}
        page.evaluate("(v) => __winnow.filterByValue('Host', v, { exclude: true })", first[0])
        page.wait_for_function("(v) => !__winnow.S.groups.some((g) => g.value === v)", arg=first[0])
        page.wait_for_function(
            """(v) => { const d = __winnow.histogramData();
                 return d && d.stack && d.stack.kind === 'groups' && !d.stack.values.includes(v)
                   && !document.getElementById('histogramPanel').classList.contains('is-outdated'); }""",
            arg=first[0], timeout=15_000)
        legend = _legend_colours(page)
        heads = _heading_colours(page)
        for v, colour in with_colour.items():
            assert legend[v] == colour and heads[v] == colour, (v, colour, legend, heads)
    finally:
        page.evaluate("""async () => { __winnow.setHistogramView('plain');
          await __winnow.dropGrouping(); await __winnow.clearAllFilters(); }""")
