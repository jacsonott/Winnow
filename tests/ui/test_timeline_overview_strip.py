"""The Timeline's overview strip: the lines over time above the list, and
the three things it does — a click on a bar takes the list there, a drag
across bars narrows the Timeline to that stretch, and a table's chip takes
that table off it.

The bars' places are read from timelineOverviewX, the function the strip
paints with, so these tests click where a bar IS rather than where a
fixed layout would put it. The server half (the bars, `first`, the range
and the tables left out) is tests/test_timeline_overview.py.
"""

from __future__ import annotations

import pytest

from winnow import defaults

pytestmark = pytest.mark.ui

EVTX_COLUMNS = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]

# No other UI fixture uses this day. A burst of 60 logons in the morning,
# so the evening's lines start off screen, and two lines in the evening.
BURST = [f"2026-05-06 08:{m:02d}:00" for m in range(60)]
DC_PRIV = "2026-05-06 20:00:00"
DC_SVC = "2026-05-06 20:05:00"
WS_EXFIL = "2026-05-07 14:00:00"


def _evtx_csv(tmp_path, name, rows):
    def row(**over):
        cells = dict.fromkeys(EVTX_COLUMNS, "")
        cells.update(over)
        return ",".join(cells[c] for c in EVTX_COLUMNS)

    path = tmp_path / name
    path.write_text(",".join(EVTX_COLUMNS) + "\n" + "".join(row(**r) + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture
def two_hosts(page, tmp_path, api):
    tables = {
        "WS01_tlov.csv": [dict(RecordNumber=str(i + 1), TimeCreated=ts, EventId="4625", Computer="WS01",
                               MapDescription="Failed logon", UserName="svc_backup")
                          for i, ts in enumerate(BURST)]
        + [dict(RecordNumber="61", TimeCreated=WS_EXFIL, EventId="5156", Computer="WS01",
                MapDescription="Windows Filtering Platform connection", UserName="")],
        "DC02_tlov.csv": [
            dict(RecordNumber="1", TimeCreated=DC_PRIV, EventId="4672", Computer="DC02",
                 MapDescription="Special privileges assigned", UserName="svc_backup"),
            dict(RecordNumber="2", TimeCreated=DC_SVC, EventId="7045", Computer="DC02",
                 MapDescription="Service installed", UserName="svc_backup"),
        ],
    }
    for name, rows in tables.items():
        page.evaluate("""([path, name]) => __winnow.post('/api/ingest/jobs/path',
             { path, name, kind: 'csv' }).then(() => __winnow.startJobsPoll())""",
                      [str(_evtx_csv(tmp_path, name, rows)), name])
    page.wait_for_function(
        """(names) => names.every((n) => __winnow.S.sources.some((s) => s.name === n))
             && !__winnow.ingestJobs.some((j) => j.status === 'running' || j.status === 'queued')""",
        arg=list(tables), timeout=30_000)
    ws, dc = page.evaluate("""() => ['WS01_tlov.csv', 'DC02_tlov.csv']
        .map((n) => __winnow.S.sources.find((s) => s.name === n).id)""")
    tag_a, tag_b = page.evaluate("() => __winnow.S.tags.map((t) => t.id).sort((a, b) => a - b).slice(0, 2)")
    try:
        api("/api/row_tags", "POST", {"pairs": [[ws, i] for i in range(1, 62)] + [[dc, 2]],
                                      "tag_id": tag_a, "on": True})
        api("/api/row_tags", "POST", {"pairs": [[dc, 1], [dc, 2]], "tag_id": tag_b, "on": True})
        page.evaluate("() => __winnow.loadSources()")
        yield {"ws": ws, "dc": dc, "tags": [tag_a, tag_b]}
    finally:
        for sid in (ws, dc):
            api(f"/api/source/{sid}", "DELETE")
        page.evaluate("""async () => {
            __winnow.S.sourceId = null; __winnow.S.view = null;
            __winnow.S.timeline.sel = null; __winnow.S.timeline.range = null;
            __winnow.S.timeline.sourcesOff.clear();
            await __winnow.loadSources();
          }""")


OVERVIEW_UP = """() => { const o = __winnow.S.timeline.overview, v = __winnow.S.timeline.view;
    return !!(o && v && o.view_id === v.view_id && o.buckets.length); }"""


def _open_timeline(page, lines=63):
    page.locator("#tabTimeline").click()
    page.wait_for_selector("#timelineview:not([hidden])")
    page.wait_for_function("(n) => (__winnow.S.timeline.view || {}).row_count === n", arg=lines, timeout=10_000)
    page.wait_for_function(OVERVIEW_UP)


def _x(page, ts):
    """Page x of a time on the strip, and the y of the middle of its plot."""
    box = page.locator("#timelineOverview canvas").bounding_box()
    x = page.evaluate("(ts) => __winnow.timelineOverviewX(__winnow.timelineEpoch(ts))", ts)
    return box["x"] + x, box["y"] + box["height"] / 2


def _selected_ts(page):
    return page.evaluate("() => (__winnow.S.timeline.sel || {}).ts || null")


def _line(page, ts):
    # By the row's own timestamp: the cell says only the time of day, and
    # the day once, at the first line of each.
    return page.locator(f"#timelineRows .timeline-row[data-ts^='{ts}']")


def test_the_legend_counts_each_line_under_its_first_tag(page, two_hosts):
    _open_timeline(page)
    names = page.evaluate("(ids) => ids.map((id) => __winnow.S.tags.find((t) => t.id === id).name)",
                          two_hosts["tags"])
    legend = page.evaluate("""() => [...document.querySelectorAll('#timelineOverview .tl-ov-item')]
        .map((n) => [...n.querySelectorAll('span:not(.swatch)')].map((s) => s.textContent))""")
    # DC02's 20:05 line carries both tags and is counted once, under the first.
    assert legend == [[names[0], "62"], [names[1], "1"]]


def test_a_click_on_a_bar_takes_the_list_to_its_first_line(page, two_hosts):
    _open_timeline(page)
    assert not _line(page, DC_PRIV).is_visible()
    x, y = _x(page, DC_SVC)
    page.mouse.click(x, y)
    # The bar holds both evening lines; the list goes to the first.
    page.wait_for_function("(ts) => (__winnow.S.timeline.sel || {}).ts === ts", arg=DC_PRIV)
    line = _line(page, DC_PRIV)
    line.wait_for()
    assert "selected" in line.get_attribute("class")
    box = line.bounding_box()
    body = page.locator("#timelineBody").bounding_box()
    assert body["y"] <= box["y"] and box["y"] + box["height"] <= body["y"] + body["height"]


def test_a_click_in_a_quiet_stretch_goes_on_to_the_next_line(page, two_hosts):
    _open_timeline(page)
    x, y = _x(page, "2026-05-07 03:00:00")
    page.mouse.click(x, y)
    page.wait_for_function("(ts) => (__winnow.S.timeline.sel || {}).ts === ts", arg=WS_EXFIL)


def test_a_drag_across_bars_narrows_the_timeline_to_that_stretch(page, two_hosts):
    _open_timeline(page)
    x0, y = _x(page, "2026-05-06 19:50:00")
    x1, _ = _x(page, "2026-05-06 20:20:00")
    page.mouse.move(x0, y)
    page.mouse.down()
    page.mouse.move((x0 + x1) / 2, y)
    page.mouse.move(x1, y)
    page.mouse.up()
    page.wait_for_function("() => (__winnow.S.timeline.view || {}).row_count === 2", timeout=10_000)
    page.wait_for_function(OVERVIEW_UP)
    lo, hi = page.evaluate("() => __winnow.S.timeline.range")
    assert lo <= DC_PRIV and DC_SVC < hi and hi <= "2026-05-06 20:30:00"
    for ts in (DC_PRIV, DC_SVC):
        _line(page, ts).wait_for()
    chip = page.locator("#timelineOverview .tl-range-chip")
    assert chip.inner_text().startswith("Wed 6 May")
    chip.locator(".tl-range-x").click()
    page.wait_for_function("() => (__winnow.S.timeline.view || {}).row_count === 63", timeout=10_000)
    assert page.locator("#timelineOverview .tl-range-chip").count() == 0


def test_a_tables_chip_takes_that_table_off_the_timeline(page, two_hosts):
    _open_timeline(page)
    page.locator("#timelineOverview .tl-ov-views .btn", has_text="Tables").click()
    page.wait_for_function("() => (__winnow.S.timeline.overview || {}).by === 'tables'")
    chip = page.locator(f"#timelineOverview .tl-table-chip[data-source-id='{two_hosts['dc']}']")
    assert chip.evaluate("(c) => [...c.querySelectorAll('span:not(.swatch)')].map((s) => s.textContent)") \
        == ["Event logs · DC02", "2"]
    chip.click()
    page.wait_for_function("() => (__winnow.S.timeline.view || {}).row_count === 61", timeout=10_000)
    assert chip.get_attribute("aria-pressed") == "false"
    assert "1 of 2 tables" in page.locator("#timelineStats").inner_text()
    assert _line(page, DC_PRIV).count() == 0
    chip.click()
    page.wait_for_function("() => (__winnow.S.timeline.view || {}).row_count === 63", timeout=10_000)
    assert chip.get_attribute("aria-pressed") == "true"


def test_the_strip_can_be_put_away_and_stays_away(page, two_hosts):
    _open_timeline(page)
    page.locator("#btnTimelineOverview").click()
    assert page.locator("#timelineOverview").is_hidden()
    page.reload()
    page.wait_for_selector("#tabTimeline", state="visible")
    # Boot reopens the case's table after #tabTimeline shows; the Timeline
    # opened before it lands would be hidden again under the test.
    page.wait_for_function("() => window.__winnow && __winnow.S.view", timeout=30_000)
    page.locator("#tabTimeline").click()
    page.wait_for_function("() => (__winnow.S.timeline.view || {}).row_count === 63", timeout=10_000)
    assert page.locator("#timelineOverview").is_hidden()
    assert page.locator("#btnTimelineOverview").get_attribute("aria-pressed") == "false"
    page.locator("#btnTimelineOverview").click()
    page.wait_for_function(OVERVIEW_UP)
    assert page.locator("#timelineOverview canvas").is_visible()
