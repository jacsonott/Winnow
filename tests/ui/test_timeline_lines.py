"""The Timeline's lines: what a line says about where it came from, and
what clicking it does.

A click used to open the row in its table, so the Timeline could not be
read without being left; it now picks the line, and double-click, Enter or
the gutter's ⤢ open it. The Type column was the file name cut by its own
width ("WKSTN-014_EvtxECmd_…"), so two hosts' event logs read the same; it
is the kind of table and the host now. And every visit to the tab rebuilds
the Timeline, which put the analyst back at the top after opening a line;
they come back on that line now.

The backend half (the view keeps its order, a page reads tags and notes as
they are, timeline_position finds the line again) is tests/test_timeline.py.
"""

from __future__ import annotations

import datetime as dt

import pytest

from winnow import defaults

pytestmark = pytest.mark.ui

EVTX_COLUMNS = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]

# No other UI fixture uses this day, so these times name exactly one line.
WS_LOGON = "2026-04-02 09:00:00"
DC_PRIV = "2026-04-02 09:02:00"
WS_PROC = "2026-04-02 09:05:00"
NOTE = "first use of svc_backup from WS01"


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
    """Two EvtxECmd tables from two hosts, three rows tagged between them
    (one with two tags and a note), all taken away again afterwards —
    dropping a table takes its tags and notes with it."""
    tables = {
        "WS01_tlsel.csv": [
            dict(RecordNumber="1", TimeCreated=WS_LOGON, EventId="4624", Computer="WS01",
                 MapDescription="Successful logon", UserName="svc_backup"),
            dict(RecordNumber="2", TimeCreated=WS_PROC, EventId="4688", Computer="WS01",
                 MapDescription="Process created", UserName="svc_backup"),
        ],
        "DC02_tlsel.csv": [
            dict(RecordNumber="1", TimeCreated=DC_PRIV, EventId="4672", Computer="DC02",
                 MapDescription="Special privileges assigned", UserName="svc_backup"),
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
    ws, dc = page.evaluate("""() => ['WS01_tlsel.csv', 'DC02_tlsel.csv']
        .map((n) => __winnow.S.sources.find((s) => s.name === n).id)""")
    tag_a, tag_b = page.evaluate("() => __winnow.S.tags.slice(0, 2).map((t) => t.id)")
    try:
        api("/api/row_tags", "POST", {"pairs": [[ws, 1], [ws, 2], [dc, 1]], "tag_id": tag_a, "on": True})
        api("/api/row_tags", "POST", {"pairs": [[dc, 1]], "tag_id": tag_b, "on": True})
        api("/api/note", "POST", {"source_id": dc, "rid": 1, "note": NOTE})
        page.evaluate("() => __winnow.loadSources()")
        yield {"ws": ws, "dc": dc, "tags": [tag_a, tag_b]}
    finally:
        for sid in (ws, dc):
            api(f"/api/source/{sid}", "DELETE")
        page.evaluate("""async () => {
            __winnow.S.sourceId = null; __winnow.S.view = null;
            __winnow.S.timeline.sel = null;
            await __winnow.loadSources();
          }""")


def _open_timeline(page):
    page.locator("#tabTimeline").click()
    page.wait_for_selector("#timelineview:not([hidden])")
    _line(page, WS_PROC).wait_for(timeout=10_000)


def _line(page, ts):
    return page.locator("#timelineRows .timeline-row", has=page.locator(".tl-col-ts", has_text=ts))


def _selected_ts(page):
    return page.evaluate("() => (__winnow.S.timeline.sel || {}).ts || null")


def test_the_type_says_the_kind_and_the_host(page, two_hosts):
    _open_timeline(page)
    for ts, host, name in [(WS_LOGON, "WS01", "WS01_tlsel.csv"), (DC_PRIV, "DC02", "DC02_tlsel.csv")]:
        line = _line(page, ts)
        badge = line.locator(".type-badge")
        # The header set's nickname, less the tool in brackets — not the file.
        assert badge.inner_text() == "Event logs"
        assert badge.get_attribute("title") == name
        assert line.locator(".tl-host").inner_text() == host
    colour = "(n) => n.style.getPropertyValue('--k')"
    ws_k = _line(page, WS_LOGON).locator(".type-badge").evaluate(colour)
    dc_k = _line(page, DC_PRIV).locator(".type-badge").evaluate(colour)
    assert ws_k.startswith("var(--series-") and dc_k.startswith("var(--series-") and ws_k != dc_k


def test_the_gutter_has_a_stripe_per_tag_and_marks_a_note(page, two_hosts):
    _open_timeline(page)
    colours = page.evaluate("(ids) => ids.map((id) => __winnow.S.tags.find((t) => t.id === id).color)",
                            two_hosts["tags"])
    stripes = "(g) => [...g.querySelectorAll('.stripe')].map((s) => s.style.background)"
    dc = _line(page, DC_PRIV).locator(".tl-col-gutter")
    ws = _line(page, WS_LOGON).locator(".tl-col-gutter")
    as_rgb = page.evaluate("""(cs) => cs.map((c) => { const d = document.createElement('div');
        d.style.background = c; return d.style.background; })""", colours)
    assert dc.evaluate(stripes) == as_rgb
    assert ws.evaluate(stripes) == as_rgb[:1]
    assert dc.locator(".tl-note-mark").get_attribute("title") == NOTE
    assert ws.locator(".tl-note-mark").count() == 0


def test_a_click_picks_the_line_and_stays_on_the_timeline(page, two_hosts):
    _open_timeline(page)
    _line(page, DC_PRIV).locator(".tl-col-body").click()
    page.wait_for_function("(ts) => (__winnow.S.timeline.sel || {}).ts === ts", arg=DC_PRIV)
    assert "selected" in _line(page, DC_PRIV).get_attribute("class")
    assert page.locator("#timelineRows .timeline-row.selected").count() == 1
    # Checking that something did NOT happen: a jump opens the table first
    # and then switches tabs, so give it the time it used to take.
    page.wait_for_timeout(300)
    assert page.evaluate("() => __winnow.S.activeTab") == "timeline"


def test_the_grids_keys_move_the_line_open_it_and_let_it_go(page, two_hosts):
    _open_timeline(page)
    _line(page, WS_LOGON).locator(".tl-col-body").click()
    page.wait_for_function("(ts) => (__winnow.S.timeline.sel || {}).ts === ts", arg=WS_LOGON)
    page.keyboard.press("j")
    page.wait_for_function("(ts) => (__winnow.S.timeline.sel || {}).ts === ts", arg=DC_PRIV)
    page.keyboard.press("ArrowDown")
    page.wait_for_function("(ts) => (__winnow.S.timeline.sel || {}).ts === ts", arg=WS_PROC)
    page.keyboard.press("k")
    page.wait_for_function("(ts) => (__winnow.S.timeline.sel || {}).ts === ts", arg=DC_PRIV)
    page.keyboard.press("Escape")
    page.wait_for_function("() => __winnow.S.timeline.sel === null")
    assert page.locator("#timelineRows .timeline-row.selected").count() == 0

    _line(page, WS_PROC).locator(".tl-col-body").click()
    page.keyboard.press("Enter")
    page.wait_for_function(
        "(id) => __winnow.S.activeTab === 'grid' && __winnow.S.sourceId === id", arg=two_hosts["ws"])


def test_opening_a_line_and_coming_back_lands_on_that_line(page, two_hosts):
    _open_timeline(page)
    # Until the grid asks where the row is, the open is still under way:
    # S.activeTab is 'grid' while the table loads, and the grid tab is
    # shown only after, so a Timeline clicked open in between was hidden
    # again under the asserts below (#timelineBody with no box).
    with page.expect_response(lambda r: "/api/row_position?" in r.url
                              and f"&source_id={two_hosts['dc']}&" in r.url):
        _line(page, DC_PRIV).locator(".tl-col-body").dblclick()
    page.wait_for_function(
        "(id) => __winnow.S.activeTab === 'grid' && __winnow.S.sourceId === id", arg=two_hosts["dc"])
    page.locator("#tabTimeline").click()
    # The tab rebuilds on the way in; the line is found again in the new
    # view, picked, and in sight. Measured in the page in one go, not by
    # two bounding_box calls: the list repaints its rows whenever a page
    # or the details pane's answer lands, and a repaint between the two
    # calls left the first measuring a detached line (None).
    page.wait_for_function("""(ts) => {
        const n = document.querySelector('#timelineRows .timeline-row.selected');
        if (!n || n.querySelector('.tl-col-ts').textContent !== ts) return false;
        const body = document.getElementById('timelineBody').getBoundingClientRect();
        const line = n.getBoundingClientRect();
        return body.height > 0 && body.top <= line.top && line.bottom <= body.bottom; }""",
                           arg=DC_PRIV, timeout=10_000)
    assert _selected_ts(page) == DC_PRIV


def test_the_open_mark_opens_the_row_in_its_table(page, two_hosts):
    _open_timeline(page)
    line = _line(page, WS_LOGON)
    line.hover()
    line.locator(".tl-row-open").click()
    page.wait_for_function(
        "(id) => __winnow.S.activeTab === 'grid' && __winnow.S.sourceId === id", arg=two_hosts["ws"])


def test_the_header_names_the_day_of_the_top_line(page, two_hosts):
    _open_timeline(page)
    top = page.locator("#timelineRows .timeline-row:not(.pending) .tl-col-ts").first.inner_text()
    day = dt.date.fromisoformat(top[:10])
    want = f"Timestamp · {day.strftime('%a')} {day.day} {day.strftime('%b')}"
    assert page.locator("#timelineColTs").text_content() == want
