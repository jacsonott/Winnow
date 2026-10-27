"""The Timeline's detail pane: the selected line in full beside the list,
tagged and annotated in place, and "What else happened then" — the
untagged rows around a finding, from every table, put on the Timeline
for as long as the window is on.

The server half (which rows are around a finding, both ways of reading
the window, the row in full) is tests/test_timeline_context.py.
"""

from __future__ import annotations

import pytest

from winnow import defaults

pytestmark = pytest.mark.ui

EVTX_COLUMNS = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]

# No other UI fixture uses this day. Two findings on WS01; around the
# first, within five minutes, one untagged row on WS01 and two on DC02.
FINDING = "2026-05-20 10:00:00"
LATER = "2026-05-20 12:00:00"
NEAR = ["2026-05-20 09:58:00", "2026-05-20 10:02:00", "2026-05-20 10:04:30"]


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
        "WS01_tlpane.csv": [
            dict(RecordNumber="1", TimeCreated=FINDING, EventId="4624", Computer="WS01",
                 MapDescription="Successful logon", UserName="svc_backup"),
            dict(RecordNumber="2", TimeCreated=NEAR[1], EventId="4688", Computer="WS01",
                 MapDescription="Process created", UserName="svc_backup"),
            dict(RecordNumber="3", TimeCreated="2026-05-20 10:20:00", EventId="4688", Computer="WS01",
                 MapDescription="Process created", UserName="alice"),
            dict(RecordNumber="4", TimeCreated=LATER, EventId="4625", Computer="WS01",
                 MapDescription="Failed logon", UserName="svc_backup"),
        ],
        "DC02_tlpane.csv": [
            dict(RecordNumber="1", TimeCreated=NEAR[0], EventId="4672", Computer="DC02",
                 MapDescription="Special privileges assigned", UserName="svc_backup"),
            dict(RecordNumber="2", TimeCreated=NEAR[2], EventId="7045", Computer="DC02",
                 MapDescription="Service installed", UserName="svc_backup"),
            dict(RecordNumber="3", TimeCreated="2026-05-20 11:30:00", EventId="7045", Computer="DC02",
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
    ws, dc = page.evaluate("""() => ['WS01_tlpane.csv', 'DC02_tlpane.csv']
        .map((n) => __winnow.S.sources.find((s) => s.name === n).id)""")
    tags = page.evaluate("""() => __winnow.S.tags.filter((t) => t.hotkey)
        .sort((a, b) => a.id - b.id).slice(0, 2).map((t) => ({ id: t.id, name: t.name, key: t.hotkey }))""")
    try:
        api("/api/row_tags", "POST", {"pairs": [[ws, 1], [ws, 4]], "tag_id": tags[0]["id"], "on": True})
        page.evaluate("() => __winnow.loadSources()")
        yield {"ws": ws, "dc": dc, "tags": tags}
    finally:
        for sid in (ws, dc):
            api(f"/api/source/{sid}", "DELETE")
        page.evaluate("""async () => {
            __winnow.S.sourceId = null; __winnow.S.view = null;
            Object.assign(__winnow.S.timeline, { sel: null, timeRange: { enabled: false, start: '', end: '' }, context: null });
            __winnow.S.timeline.sourcesOff.clear();
            await __winnow.loadSources();
          }""")


def _open_timeline(page, lines=2):
    page.locator("#tabTimeline").click()
    page.wait_for_selector("#timelineview:not([hidden])")
    page.wait_for_function("(n) => (__winnow.S.timeline.view || {}).row_count === n", arg=lines, timeout=10_000)


def _line(page, ts):
    # By the row's own timestamp, which is also what the cell says.
    return page.locator(f"#timelineRows .timeline-row[data-ts^='{ts}']")


def _pane_shows(page, ts):
    """The pane has read the row of the line at `ts`: its fields are in."""
    page.wait_for_function("""(ts) => {
        const p = document.getElementById('timelinePane');
        return !p.hidden && p.querySelector('.tl-pane-cols dd[data-col="TimeCreated"]')
          && p.querySelector('.tl-pane-cols dd[data-col="TimeCreated"]').textContent.includes(ts);
      }""", arg=ts)


def _not_around(page, ts):
    """Waits for the line at `ts` to be drawn as a finding rather than a
    row around one. The view lands a step before the rows repaint (the
    selected line's place is looked up in between), so reading the class
    as soon as S.timeline.view changes can read the old list."""
    page.wait_for_function("""(ts) => {
        const r = document.querySelector(`#timelineRows .timeline-row[data-ts^='${ts}']`);
        return !!r && !r.classList.contains('ctx');
      }""", arg=ts, timeout=10_000)


def _head(page):
    return page.evaluate("() => document.querySelector('#timelinePane .tl-pane-head span').textContent")


def _chip(page, tag):
    return page.locator(f"#timelinePane .tl-pane-tags .tag-chip[data-tag-id='{tag['id']}']")


def test_a_picked_line_is_shown_in_full_beside_the_list(page, two_hosts, copied):
    _open_timeline(page)
    page.wait_for_selector("#timelinePane .tl-pane-empty")
    assert _head(page) == "Details"
    _line(page, FINDING).click()
    _pane_shows(page, FINDING)
    assert _head(page) == "Finding"
    # The whole ISO timestamp, as the line says it.
    assert page.locator("#timelinePane .tl-pane-when b").inner_text() == FINDING
    assert page.locator("#timelinePane .tl-pane-lead").inner_text() == "Successful logon"
    assert _chip(page, two_hosts["tags"][0]).get_attribute("aria-pressed") == "true"
    assert _chip(page, two_hosts["tags"][1]).get_attribute("aria-pressed") == "false"
    # Every column the row has a value in, and only those.
    names = page.evaluate("() => [...document.querySelectorAll('#timelinePane .tl-pane-cols dt')].map((d) => d.textContent)")
    assert {"RecordNumber", "TimeCreated", "EventId", "Computer", "MapDescription", "UserName"} == set(names)
    dd = page.locator("#timelinePane .tl-pane-cols dd[data-col='UserName']")
    dd.hover()
    with copied(page) as clip:
        dd.locator(".dfield-copy").click()
    assert clip.text == "svc_backup"
    # Esc lets go of the line; the pane stays where it is, empty.
    page.locator("#timelineBody").focus()
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.querySelector('#timelinePane .tl-pane-head span').textContent === 'Details'")
    assert page.locator("#timelinePane").is_visible()


def test_the_pane_does_not_move_the_list_under_a_double_click(page, two_hosts):
    """The first click of a double-click picks the line. A pane that
    opened on it narrowed the list, and the second click landed on
    whatever had moved under the pointer, so the row never opened."""
    _open_timeline(page)
    page.wait_for_selector("#timelinePane .tl-pane-empty")
    before = page.locator("#timelineBody").bounding_box()
    _line(page, LATER).click()
    _pane_shows(page, LATER)
    assert page.locator("#timelineBody").bounding_box() == before


def test_details_puts_the_pane_away_and_it_stays_away(page, two_hosts):
    _open_timeline(page)
    _line(page, LATER).click()
    _pane_shows(page, LATER)
    try:
        page.locator("#btnTimelinePane").click()
        assert page.locator("#timelinePane").is_hidden()
        page.reload()
        page.wait_for_selector("#tabTimeline", state="visible")
        # Boot reopens the case's table after #tabTimeline shows; the Timeline
        # opened before it lands would be hidden again under the test.
        page.wait_for_function("() => window.__winnow && __winnow.S.view", timeout=30_000)
        _open_timeline(page)
        assert page.locator("#timelinePane").is_hidden()
        assert page.locator("#btnTimelinePane").get_attribute("aria-pressed") == "false"
        page.locator("#btnTimelinePane").click()
        page.wait_for_selector("#timelinePane .tl-pane-empty")
        _line(page, LATER).click()
        _pane_shows(page, LATER)
    finally:
        page.evaluate("() => localStorage.removeItem('winnow.timelinePane')")


def test_the_tag_keys_tag_the_line_the_pane_shows_and_undo_takes_it_back(page, two_hosts, tag_written):
    _open_timeline(page)
    second = two_hosts["tags"][1]
    _line(page, LATER).click()
    _pane_shows(page, LATER)
    with tag_written(page):
        page.keyboard.press(second["key"])
    page.wait_for_function("(sel) => document.querySelector(sel).getAttribute('aria-pressed') === 'true'",
                           arg=f"#timelinePane .tag-chip[data-tag-id='{second['id']}']")
    row = page.evaluate(f"() => __winnow.api('/api/timeline_row?source_id={two_hosts['ws']}&rid=4')")
    assert second["id"] in row["tags"]
    # Still the same line, and it says so in the list too.
    assert page.evaluate("() => __winnow.S.timeline.sel.ts") == LATER
    _line(page, LATER).locator(".stripe").nth(1).wait_for()
    page.keyboard.press("Control+z")
    page.wait_for_function("(sel) => document.querySelector(sel).getAttribute('aria-pressed') === 'false'",
                           arg=f"#timelinePane .tag-chip[data-tag-id='{second['id']}']")
    row = page.evaluate(f"() => __winnow.api('/api/timeline_row?source_id={two_hosts['ws']}&rid=4')")
    assert second["id"] not in row["tags"]


def test_what_else_happened_then_brings_in_the_rows_around_a_finding(page, two_hosts, tag_written):
    _open_timeline(page)
    _line(page, FINDING).click()
    _pane_shows(page, FINDING)
    page.locator("#timelinePane .tl-pane-seg .btn", has_text="± 5 min").click()
    page.wait_for_function("() => (__winnow.S.timeline.view || {}).context_rows === 3", timeout=10_000)
    for ts in NEAR:
        _line(page, ts).wait_for()
        assert "ctx" in _line(page, ts).get_attribute("class")
    # 10:20 and 11:30 are outside the window; the two findings are lines.
    assert page.evaluate("() => __winnow.S.timeline.view.row_count") == 5
    assert "ctx" not in _line(page, FINDING).get_attribute("class")
    assert page.locator("#timelineStats").inner_text().startswith("2 tagged rows")
    chip = page.locator("#timelineContext .tl-context-chip")
    assert chip.inner_text().replace("\n", " ").startswith(f"± 5 min around {FINDING} · 3 untagged")
    # Still on the finding, and its window button says it is the one on.
    _pane_shows(page, FINDING)
    assert page.locator("#timelinePane .tl-pane-seg .btn", has_text="± 5 min").get_attribute("aria-pressed") == "true"

    # A row around it, tagged from the pane, becomes a finding of its own.
    _line(page, NEAR[2]).click()
    _pane_shows(page, NEAR[2])
    assert _head(page) == "Untagged row"
    first = two_hosts["tags"][0]
    with tag_written(page):
        page.keyboard.press(first["key"])
    page.wait_for_function("() => __winnow.S.timeline.view.context_rows === 2", timeout=10_000)
    page.wait_for_function("() => document.querySelector('#timelinePane .tl-pane-head span').textContent === 'Finding'")
    _not_around(page, NEAR[2])

    # ✕ takes the rows around off again; the one tagged stays.
    chip.locator(".tl-range-x").click()
    page.wait_for_function("() => (__winnow.S.timeline.view || {}).row_count === 3", timeout=10_000)
    assert page.locator("#timelineContext .tl-context-chip").count() == 0
    page.wait_for_function("() => !document.querySelector('#timelineRows .timeline-row.ctx')", timeout=10_000)
    _line(page, NEAR[2]).wait_for()


def test_a_note_typed_in_the_pane_is_kept(page, two_hosts):
    _open_timeline(page)
    _line(page, LATER).click()
    _pane_shows(page, LATER)
    page.locator("#timelineNote").fill("lockout after the logon")
    page.wait_for_function("() => document.getElementById('timelineNoteStatus').textContent === 'Saved'")
    row = page.evaluate(f"() => __winnow.api('/api/timeline_row?source_id={two_hosts['ws']}&rid=4')")
    assert row["note"] == "lockout after the logon"
    _line(page, LATER).locator(".tl-note-mark").wait_for()
