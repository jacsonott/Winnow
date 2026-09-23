"""Colouring the histogram by tag.

The chart answers "when did the rows in this view happen"; with tagging
underway the more useful question is which of them you have already
marked, and where the ones you have not are. Same bars, coloured.

One row, one segment (the server decides, under the first tag in ribbon
order), so the bars stay exactly as tall as the plain chart's — which is
what makes the toggle a way of reading the same chart rather than a
different chart.

The session case is shared: this tags rows and takes the tags back off.
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


@pytest.fixture
def tagged_rows(page, api):
    """Rows 1–20 under the first tag, 21–30 under the second."""
    tags = page.evaluate("() => __winnow.S.tags.slice(0, 2).map((t) => ({id: t.id, name: t.name}))")
    assert len(tags) >= 2, "the fixture case needs two tags"
    src = page.evaluate("() => __winnow.S.sourceId")
    api("/api/row_tags", "POST", {"source_id": src, "rids": list(range(1, 21)),
                                  "tag_id": tags[0]["id"], "on": True})
    api("/api/row_tags", "POST", {"source_id": src, "rids": list(range(21, 31)),
                                  "tag_id": tags[1]["id"], "on": True})
    page.evaluate("() => __winnow.loadTags()")
    try:
        yield tags
    finally:
        for t, rids in ((tags[0], range(1, 21)), (tags[1], range(21, 31))):
            api("/api/row_tags", "POST", {"source_id": src, "rids": list(rids),
                                          "tag_id": t["id"], "on": False})
        page.evaluate("""() => { __winnow.toggleHistogramStack(false);
          return __winnow.loadTags(); }""")


def test_the_toggle_colours_the_bars_by_tag(page, tagged_rows):
    a, b = tagged_rows
    try:
        _open_strip(page)
        assert _legend(page) == [], "the plain chart has nothing to put in a legend"
        page.locator("#histogramPanel .th-stack").click()
        page.wait_for_function("() => document.querySelectorAll('.th-legend-item').length > 0",
                               timeout=15_000)
        # Untagged first — it is the base of every bar — then the tags that
        # are actually in this view, in ribbon order.
        assert _legend(page) == ["Untagged", a["name"], b["name"]]
        assert page.locator("#histogramPanel .th-stack").get_attribute("aria-pressed") == "true"
    finally:
        page.evaluate("() => __winnow.toggleHistogramStack(false)")


def test_the_bars_stay_as_tall_as_the_plain_chart(page, tagged_rows):
    """The segments of a bucket sum to that bucket's count. If they did
    not, the stacked chart would be describing a different number of rows
    from the one beside it in the count line."""
    try:
        _open_strip(page)
        page.evaluate("() => __winnow.toggleHistogramStack(true)")
        page.wait_for_function("() => document.querySelectorAll('.th-legend-item').length > 0",
                               timeout=15_000)
        got = page.evaluate("""() => {
          const d = __winnow.histogramData();
          const flat = new Map(d.buckets);
          return d.stack.buckets.map(([b, counts]) =>
            [counts.reduce((a, c) => a + c, 0), flat.get(b)]);
        }""")
        assert got, "nothing was charted"
        assert all(seg == whole for seg, whole in got), got
    finally:
        page.evaluate("() => __winnow.toggleHistogramStack(false)")


def test_the_choice_is_remembered_per_browser(page, tagged_rows):
    """Like the open flag and the chosen column — it is a way of reading,
    not something about this case."""
    try:
        _open_strip(page)
        page.evaluate("() => __winnow.toggleHistogramStack(true)")
        page.wait_for_function("() => document.querySelectorAll('.th-legend-item').length > 0",
                               timeout=15_000)
        assert page.evaluate(
            "() => JSON.parse(localStorage.getItem('winnow.histogram') || '{}').stack") is True
        page.reload()
        page.wait_for_function("() => window.__winnow && __winnow.S.view", timeout=30_000)
        page.wait_for_function("() => document.querySelectorAll('.th-legend-item').length > 0",
                               timeout=15_000)
        assert page.locator("#histogramPanel .th-stack").get_attribute("aria-pressed") == "true"
    finally:
        page.evaluate("() => __winnow.toggleHistogramStack(false)")


def test_a_tag_deleted_under_the_chart_folds_into_untagged(page, tagged_rows):
    """The rows are still in the view, so the bar must not shrink — the
    segment has no colour to draw in any more, and that is all."""
    try:
        _open_strip(page)
        page.evaluate("() => __winnow.toggleHistogramStack(true)")
        page.wait_for_function("() => document.querySelectorAll('.th-legend-item').length > 0",
                               timeout=15_000)
        got = page.evaluate("""() => {
          // The answer on screen, with one of its tags no longer defined.
          const before = __winnow.histogramData();
          const gone = before.stack.tags[0];
          const kept = __winnow.S.tags;
          __winnow.S.tags = kept.filter((t) => t.id !== gone);
          const total = before.stack.buckets.reduce((a, [, c]) => a + c.reduce((x, y) => x + y, 0), 0);
          const layers = __winnow.histogramLayers();
          __winnow.S.tags = kept;
          return { total, folded: layers.rows.reduce((a, [, c]) => a + c.reduce((x, y) => x + y, 0), 0),
                   labels: layers.labels };
        }""")
        assert got["folded"] == got["total"], "rows went missing when a tag did"
        assert got["labels"][0] == "Untagged"
    finally:
        page.evaluate("() => __winnow.toggleHistogramStack(false)")
