"""Where the grid is, on the histogram.

The strip has always charted the whole view; scrolling moved through it
with nothing on the chart to say so. The marker is the answer — a bracket
over the span the on-screen rows cover, with a tick per row — and the
thing it must never do is point at the wrong place, which for a chart
whose x axis is epoch seconds means agreeing with the server about what
a timestamp string means.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_histogram(page):
    if not page.evaluate("() => __winnow.histogramOpen()"):
        page.evaluate("() => __winnow.toggleHistogram(true)")
    page.wait_for_selector("#histogramPanel:not([hidden])", timeout=10_000)
    page.wait_for_function("() => !!__winnow.histogramData()", timeout=20_000)


def _mark(page):
    return page.evaluate("() => __winnow.histogramViewport()")


def _info(page):
    return page.evaluate("() => (document.querySelector('#histogramPanel .th-info') || {}).textContent || ''")


def _scroll_to(page, frac):
    page.evaluate("(f) => { const b = document.getElementById('body'); b.scrollTop = b.scrollHeight * f; }",
                  arg=frac)
    page.wait_for_timeout(250)


@pytest.fixture(autouse=True)
def _reset(page):
    _open_histogram(page)
    yield
    page.evaluate("""async () => {
      __winnow.S.groupByCols = [];
      __winnow.S.sort = [];
      await __winnow.rebuildView();
      document.getElementById('body').scrollTop = 0;
    }""")


def test_the_marker_covers_the_rows_that_are_on_screen(page):
    _scroll_to(page, 0)
    m = _mark(page)
    assert m, "no viewport marker at the top of the table"
    assert m["rows"] > 1 and m["times"], m
    # Every epoch it places is inside the span the chart was drawn over.
    data = page.evaluate("() => __winnow.histogramData()")
    t0 = data["buckets"][0][0]
    t1 = data["buckets"][-1][0] + data["bucket_seconds"]
    assert t0 <= m["t0"] <= m["t1"] <= t1, (m["t0"], m["t1"], t0, t1)


def test_it_agrees_with_the_server_about_what_a_timestamp_means(page):
    """The bars are bucketed on SQLite's strftime('%s'), which reads the
    normalised string as UTC. Parse the same string as LOCAL time and every
    marker silently shifts by the machine's offset — a position cue that is
    confidently wrong, which is worse than none. Pinned by round-tripping
    the first on-screen row's own cell text through the marker."""
    _scroll_to(page, 0)
    first = page.evaluate("""() => {
      const r = __winnow.rowAt(0);
      const i = __winnow.S.columns.findIndex((c) => c.name === 'Timestamp');
      return r ? r.cells[i] : null;
    }""")
    assert first, "no row 0 to read"
    m = _mark(page)
    # The fixture's rows ascend in time and row 0 is the earliest on screen.
    assert f"on screen {first}" in _info(page), (first, _info(page))
    assert m["t0"] == page.evaluate("(s) => Date.parse(s.replace(' ', 'T') + 'Z') / 1000", arg=first)


def test_scrolling_moves_it_forward(page):
    _scroll_to(page, 0)
    top = _mark(page)
    _scroll_to(page, 1.0)
    end = _mark(page)
    assert end, "the marker vanished at the end of the table"
    assert end["t0"] > top["t0"], (top["t0"], end["t0"])
    assert end["first"] > top["first"]
    # …and the info line says so, so the marker is readable without
    # measuring pixels.
    assert "on screen" in _info(page)


def test_a_page_still_in_flight_is_not_counted_as_zero(page):
    """A row whose page has not landed has no timestamp yet. Counting it as
    epoch 0 would drag the band to 1970 and off the left edge; it is
    reported as unread instead, and the band is drawn from what is known."""
    m = page.evaluate("""() => {
      // Hide every loaded row from rowAt but one, the way an unlanded page
      // looks, and read the marker through the same path the canvas does.
      const saved = __winnow.S.rowsByPos;
      const keep = new Map();
      const first = saved.keys().next();
      if (!first.done) keep.set(first.value, saved.get(first.value));
      __winnow.S.rowsByPos = keep;
      const out = __winnow.histogramViewport();
      __winnow.S.rowsByPos = saved;
      return out;
    }""")
    assert m, "one known row should still produce a marker"
    assert m["unread"] > 0, m
    assert len(m["times"]) == 1
    assert m["t0"] == m["t1"] == m["times"][0]
    assert m["t0"] > 0, "an unlanded row was counted as epoch zero"


def test_grouped_mode_has_no_marker(page):
    """Group headers interleave with rows and a collapsed group's rows are
    not loaded at all, so there is no honest "these rows are on screen"."""
    page.evaluate("""async () => {
      __winnow.S.groupByCols = ['Host'];
      await __winnow.rebuildView();
    }""")
    page.wait_for_function("() => __winnow.S.groupByCols.length === 1", timeout=10_000)
    assert _mark(page) is None
    # The info line is repainted on the strip's own debounce, so this waits
    # for it to catch up rather than sampling it mid-flight — and still
    # fails, by timing out, if it never does.
    page.wait_for_function(
        "() => !(document.querySelector('#histogramPanel .th-info') || {}).textContent.includes('on screen')",
        timeout=10_000)


def test_it_is_gone_off_the_grid(page):
    """The marker is about the grid's viewport. On the SQL page there isn't
    one, and a marker left pointing at where the grid used to be would be a
    claim about a surface nobody is looking at."""
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])", timeout=10_000)
    try:
        assert _mark(page) is None
    finally:
        page.evaluate("() => __winnow.showGridTab()")
        page.wait_for_function("() => __winnow.S.activeTab === 'grid'", timeout=10_000)


def test_the_epochs_are_utc_even_when_the_browser_is_not(browser, server, first_run_init):
    """The test above cannot catch a local-time parse on a UTC machine, and
    every CI box here is UTC. This one runs the page in New York: parse the
    fixture's `2026-03-14 08:00:00` as local and the marker lands four
    hours off, which is a marker pointing confidently at the wrong bar."""
    ctx = browser.new_context(timezone_id="America/New_York")
    ctx.add_init_script(first_run_init)
    pg = ctx.new_page()
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_selector(".row", timeout=30_000)
        # Prove the context really is offset, or this asserts nothing.
        assert pg.evaluate("() => new Date(2026, 2, 14, 8, 0, 0).getTimezoneOffset()") != 0
        if not pg.evaluate("() => __winnow.histogramOpen()"):
            pg.evaluate("() => __winnow.toggleHistogram(true)")
        pg.wait_for_function("() => !!__winnow.histogramData()", timeout=20_000)
        pg.evaluate("() => { document.getElementById('body').scrollTop = 0; }")
        pg.wait_for_timeout(300)
        m = pg.evaluate("() => __winnow.histogramViewport()")
        assert m, "no marker"
        raw = pg.evaluate("""() => {
          const r = __winnow.rowAt(0);
          const i = __winnow.S.columns.findIndex((c) => c.name === 'Timestamp');
          return r ? r.cells[i] : null;
        }""")
        # Computed in Python, so the assertion does not go through the same
        # Date the code under test uses.
        import datetime
        want = int(datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=datetime.timezone.utc).timestamp())
        assert m["t0"] == want, (raw, m["t0"], want)
        # And it lands inside the span the server bucketed, which is the
        # thing an offset would break.
        data = pg.evaluate("() => __winnow.histogramData()")
        assert data["buckets"][0][0] <= m["t0"], (m["t0"], data["buckets"][0][0])
    finally:
        ctx.close()
