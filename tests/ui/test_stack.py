"""Stack view — rarest-first value counts for a column, drawn as a chart
of one bar per value, click-to-filter. Reached from the column header
menu; the table's other columns are down the dialog's side list."""

from __future__ import annotations

import json
import re

import pytest

pytestmark = pytest.mark.ui

SUMMARY = re.compile(r".*/api/group_summary.*")

# Each bar, as the chart drew it: its value, its count, and the bar's
# length as a share of the track it sits in.
BARS = """() => [...document.querySelectorAll('#modal .stack-row')].map((r) => {
  const fill = r.querySelector('.stack-fill'), track = r.querySelector('.stack-track');
  const room = track.clientWidth - parseFloat(getComputedStyle(track).paddingRight);
  return { value: r.dataset.value, count: +r.dataset.count, share: fill.getBoundingClientRect().width / room,
           height: r.getBoundingClientRect().height };
})"""


def _open_stack(page, column):
    page.evaluate("(c) => __winnow.openStack(c)", column)
    page.wait_for_selector("#modal:not([hidden])")
    page.wait_for_function("() => document.querySelector('#modal .stack-row, #modal .stack-empty:not(:empty)')"
                           " && !document.querySelector('#modal .stack-chart.stale')", timeout=10_000)


def _close(page):
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_stack_lists_values_rarest_first_and_filters(page, view_rebuilt):
    _open_stack(page, "EventId")
    try:
        assert page.locator("#modalTitle").inner_text().lower() == "stack — eventid"
        assert page.locator("#modal .mb-seg button", has_text="Rarest first").get_attribute("aria-pressed") == "true"
        bars = page.evaluate(BARS)
        assert [b["value"] for b in bars] and len(bars) == 4, bars
        counts = [b["count"] for b in bars]
        assert counts == sorted(counts), counts
        assert sum(counts) == 200
        # The figures over the chart.
        stats = page.locator("#modal .mb-stats").inner_text().lower()
        assert "4\ndistinct values" in stats and "200\nrows counted" in stats, stats
        # Most common turns it round.
        page.locator("#modal .mb-seg button", has_text="Most common").click()
        page.wait_for_function("() => document.querySelector('#modal .mb-seg [data-key=desc]').getAttribute('aria-pressed') === 'true'"
                               " && !document.querySelector('#modal .stack-chart.stale')")
        counts = [b["count"] for b in page.evaluate(BARS)]
        assert counts == sorted(counts, reverse=True), counts
        # A click on a bar filters the grid to its value.
        value = page.locator("#modal .stack-row").first.get_attribute("data-value")
        with view_rebuilt(page):
            page.locator("#modal .stack-row [data-role=bar]").first.click()
        assert page.locator("#modal").is_hidden()
        assert page.evaluate("() => __winnow.S.view.row_count") == 50
        assert value in page.evaluate("() => __winnow.S.filters.EventId")
    finally:
        page.evaluate("() => __winnow.clearAllFilters()")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")


def test_bars_are_drawn_to_one_scale_that_ends_at_the_largest(page):
    """The longest bar spans the chart and every other one is its count's
    share of it, against gridlines on round numbers."""
    groups = [{"value": "rare", "count": 3}, {"value": "some", "count": 41}, {"value": "most", "count": 412}]
    page.route(SUMMARY, lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"groups": groups})))
    try:
        _open_stack(page, "Host")
        bars = {b["value"]: b for b in page.evaluate(BARS)}
        assert abs(bars["most"]["share"] - 1) < 0.01, bars
        assert abs(bars["some"]["share"] - 41 / 412) < 0.01, bars
        ticks = page.locator("#modal .stack-tick").all_inner_texts()
        assert ticks == ["0", "100", "200", "300", "400"], ticks
    finally:
        _close(page)
        page.unroute(SUMMARY)


def test_a_few_values_draw_broad_bars_and_a_long_tail_scrolls(page):
    """Five values share the chart's height out as five broad bars, not five
    slivers in the corner of a box; two hundred stay readable and scroll.

    The long tail is stubbed: every column in the shared fixture holds a
    handful of distinct values, and what decides the bars' height is how
    many there are."""
    _open_stack(page, "Host")
    try:
        few = page.evaluate(BARS)
        assert len(few) == 5 and all(b["height"] >= 40 for b in few), few
    finally:
        _close(page)
    groups = [{"value": f"v{i:03d}", "count": 1 + i} for i in range(200)]
    page.route(SUMMARY, lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"groups": groups})))
    try:
        _open_stack(page, "Host")
        many = page.evaluate(BARS)
        assert len(many) == 200 and all(26 <= b["height"] <= 30 for b in many), many[:3]
        box = page.evaluate("() => { const s = document.querySelector('#modal .stack-scroll'); return [s.scrollHeight, s.clientHeight]; }")
        assert box[0] > box[1], box   # the rest is reachable by scrolling
    finally:
        _close(page)
        page.unroute(SUMMARY)


def test_a_count_cut_short_says_so(page):
    """Past the bars it draws, or past the values the server counts, the
    chart's foot says what is missing rather than letting the tail end
    look like the column's end."""
    groups = [{"value": f"v{i:03d}", "count": 1} for i in range(600)]
    page.route(SUMMARY, lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"groups": groups})))
    try:
        _open_stack(page, "Host")
        assert page.locator("#modal .stack-row").count() == 500
        assert "Drawing the rarest 500 of 600" in page.locator("#modal .stack-readout").inner_text()
    finally:
        _close(page)
        page.unroute(SUMMARY)
    page.route(SUMMARY, lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"groups": groups[:50], "truncated": True})))
    try:
        _open_stack(page, "Host")
        assert "50+" in page.locator("#modal .mb-stats").inner_text()
        assert "the column holds more" in page.locator("#modal .stack-readout").inner_text()
    finally:
        _close(page)
        page.unroute(SUMMARY)


def test_the_side_list_restacks_and_the_keyboard_walks_the_bars(page, view_rebuilt):
    _open_stack(page, "Host")
    try:
        page.locator("#modal .side-rail-item[data-key=EventId]").click()
        page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Stack — EventId'"
                               " && document.querySelectorAll('#modal .stack-row').length === 4"
                               " && !document.querySelector('#modal .stack-chart.stale')")
        bars = page.locator("#modal .stack-row [data-role=bar]")
        bars.first.focus()
        page.keyboard.press("ArrowDown")
        second = page.locator("#modal .stack-row").nth(1).get_attribute("data-value")
        assert page.evaluate("() => document.activeElement.closest('.stack-row').dataset.value") == second
        # what the focused bar is, said in the chart's foot
        assert page.locator("#modal .stack-readout").inner_text().startswith(second + " — 50 rows")
        with view_rebuilt(page):
            page.keyboard.press("Enter")
        assert page.evaluate("() => __winnow.S.view.row_count") == 50
    finally:
        page.evaluate("() => __winnow.clearAllFilters()")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")


def test_a_bars_menu_excludes_its_value(page, view_rebuilt):
    _open_stack(page, "Host")
    try:
        row = page.locator("#modal .stack-row").first
        row.locator("[data-role=more]").click()
        with view_rebuilt(page):
            page.locator(".menu .menu-item", has_text="Exclude this value").click()
        assert page.evaluate("() => __winnow.S.view.row_count") == 160
    finally:
        page.evaluate("() => __winnow.clearAllFilters()")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
