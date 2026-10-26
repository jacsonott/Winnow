"""The Timeline's timeframe filter: the table pages' ⏱ Timeframe, on the
Timeline — the button, the dialog (Start and End typed, either optional,
both kept), the chip that says the range in ISO with ✕ to clear it, and
the same two keys, which act on the Timeline's own range while it is the
page showing. It does not follow the tables' timeframe; the dialog offers
that range to copy in.

The overview's drag writes the same range (test_timeline_overview_strip.py);
the server half, which reads the bounds the way the grid's timeframe
does, is tests/test_timeline_overview.py.
"""

from __future__ import annotations

import pytest

from winnow import defaults

pytestmark = pytest.mark.ui

EVTX_COLUMNS = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]

# No other UI fixture uses these days.
ROWS = [
    "2026-07-09 06:00:00",
    "2026-07-09 08:59:59",
    "2026-07-09 09:00:00",
    "2026-07-09 09:30:00",
    "2026-07-09 10:00:00",
    "2026-07-10 01:00:00",
]
NO_RANGE = "{ enabled: false, start: '', end: '' }"


@pytest.fixture
def findings(page, tmp_path, api):
    def row(i, ts):
        cells = dict.fromkeys(EVTX_COLUMNS, "")
        cells.update(RecordNumber=str(i), TimeCreated=ts, EventId="4624", Computer="WS09",
                     MapDescription="Successful logon", UserName="svc_backup")
        return ",".join(cells[c] for c in EVTX_COLUMNS)

    name = "WS09_tltimeframe.csv"
    path = tmp_path / name
    path.write_text(",".join(EVTX_COLUMNS) + "\n"
                    + "".join(row(i, ts) + "\n" for i, ts in enumerate(ROWS, 1)), encoding="utf-8")
    page.evaluate("""([path, name]) => __winnow.post('/api/ingest/jobs/path',
         { path, name, kind: 'csv' }).then(() => __winnow.startJobsPoll())""", [str(path), name])
    page.wait_for_function(
        """(name) => __winnow.S.sources.some((s) => s.name === name)
             && !__winnow.ingestJobs.some((j) => j.status === 'running' || j.status === 'queued')""",
        arg=name, timeout=30_000)
    sid = page.evaluate("(n) => __winnow.S.sources.find((s) => s.name === n).id", name)
    tag = page.evaluate("() => __winnow.S.tags.slice().sort((a, b) => a.id - b.id)[0].id")
    try:
        api("/api/row_tags", "POST", {"pairs": [[sid, i] for i in range(1, len(ROWS) + 1)], "tag_id": tag, "on": True})
        page.evaluate("() => __winnow.loadSources()")
        yield sid
    finally:
        api(f"/api/source/{sid}", "DELETE")
        page.evaluate(f"""async () => {{
            __winnow.S.sourceId = null; __winnow.S.view = null;
            Object.assign(__winnow.S.timeline, {{ sel: null, timeRange: {NO_RANGE}, context: null }});
            await __winnow.loadSources();
          }}""")


def _open_timeline(page):
    page.locator("#tabTimeline").click()
    page.wait_for_selector("#timelineview:not([hidden])")
    _wait_lines(page, ROWS)


def _wait_lines(page, stamps):
    """The Timeline built and painted with exactly these lines."""
    page.wait_for_function("(n) => (__winnow.S.timeline.view || {}).row_count === n", arg=len(stamps), timeout=10_000)
    page.wait_for_function(
        """(want) => JSON.stringify([...document.querySelectorAll('#timelineRows .timeline-row:not(.pending)')]
             .map((r) => r.dataset.ts)) === JSON.stringify(want)""", arg=stamps, timeout=10_000)


def _dialog(page):
    page.locator("#btnTimelineTimeRange").click()
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").text_content() == "Timeframe filter"
    boxes = page.locator("#modalBody .dlg-pair input")
    return boxes.nth(0), boxes.nth(1)


def _apply(page, start, end):
    s, e = _dialog(page)
    s.fill(start)
    e.fill(end)
    page.locator("#modalBody .btn", has_text="Apply").click()


def _chip(page):
    return page.locator("#timelineTimeRange .tl-range-chip")


def _key(page, key):
    # From the list, as an analyst reading it would: not from a box.
    page.locator("#timelineBody").focus()
    page.keyboard.press(key)


def test_the_dialog_narrows_the_timeline_and_keeps_both_ends(page, findings):
    _open_timeline(page)
    assert page.locator("#btnTimelineTimeRange").get_attribute("aria-pressed") == "false"
    assert _chip(page).count() == 0
    _apply(page, "2026-07-09 09:00:00", "2026-07-09 10:00:00")
    page.wait_for_selector("#modal", state="hidden")
    # A line at exactly either end is in, as on the grid.
    _wait_lines(page, ROWS[2:5])
    assert page.locator("#btnTimelineTimeRange").get_attribute("aria-pressed") == "true"
    assert _chip(page).inner_text().startswith("2026-07-09 09:00:00 → 2026-07-09 10:00:00")
    # The chip is the header's, so it is there with the overview hidden.
    page.locator("#btnTimelineOverview").click()
    page.wait_for_selector("#timelineOverview", state="hidden")
    assert _chip(page).is_visible()
    page.locator("#btnTimelineOverview").click()

    # Either end can be left open.
    _apply(page, "", "2026-07-09 08:59:59")
    _wait_lines(page, ROWS[:2])
    assert _chip(page).inner_text().startswith("… → 2026-07-09 08:59:59")


def test_what_was_typed_is_read_as_the_grids_timeframe_reads_it_and_kept_in_iso(page, findings):
    _open_timeline(page)
    # A US date, and a date alone, which is midnight.
    _apply(page, "7/9/2026 9:30 AM", "2026-07-10")
    _wait_lines(page, ROWS[3:5])
    tr = page.evaluate("() => __winnow.S.timeline.timeRange")
    assert (tr["start"], tr["end"]) == ("2026-07-09 09:30:00", "2026-07-10 00:00:00")
    assert _chip(page).inner_text().startswith("2026-07-09 09:30:00 → 2026-07-10 00:00:00")
    # The dialog opens on it as ISO text too.
    s, e = _dialog(page)
    assert (s.input_value(), e.input_value()) == ("2026-07-09 09:30:00", "2026-07-10 00:00:00")

    # Text that is not a timestamp is refused in the dialog rather than
    # built into a Timeline that matches nothing.
    s.fill("yesterday")
    page.locator("#modalBody .btn", has_text="Apply").click()
    page.wait_for_function("() => /Start: not a recognized timestamp/.test(document.body.textContent)")
    assert page.locator("#modal").is_visible()
    assert page.evaluate("() => __winnow.S.timeline.timeRange.start") == "2026-07-09 09:30:00"
    page.locator("#modalBody .btn", has_text="Cancel").click()
    _wait_lines(page, ROWS[3:5])


def test_the_timeframe_keys_turn_it_off_and_on_and_clear_forgets_it(page, findings):
    _open_timeline(page)
    # The toggle key with no range set opens the dialog to set one.
    _key(page, "r")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modalBody .btn", has_text="Cancel").click()

    # A range that leaves no line: the list says why, and the key still
    # works on the empty list.
    _apply(page, "2026-07-11 00:00:00", "")
    page.wait_for_function("() => (__winnow.S.timeline.view || {}).row_count === 0", timeout=10_000)
    assert "No tagged rows in this timeframe" in page.locator("#timelineEmpty").inner_text()
    _key(page, "r")
    _wait_lines(page, ROWS)
    # Off keeps the range: the button is up, the chip gone, and the
    # dialog says it is off.
    assert page.locator("#btnTimelineTimeRange").get_attribute("aria-pressed") == "false"
    assert _chip(page).count() == 0
    assert page.evaluate("() => __winnow.S.timeline.timeRange.start") == "2026-07-11 00:00:00"
    _key(page, "R")
    page.wait_for_selector("#modal:not([hidden])")
    assert "Off right now — Apply turns it back on." in page.locator("#modalBody").inner_text()
    # The key that opened the dialog closes it, as on the table pages.
    page.keyboard.press("R")
    page.wait_for_selector("#modal", state="hidden")
    _key(page, "r")
    page.wait_for_function("() => (__winnow.S.timeline.view || {}).row_count === 0", timeout=10_000)

    # Clear forgets it.
    _dialog(page)
    page.locator("#modalBody .btn", has_text="Clear").click()
    _wait_lines(page, ROWS)
    assert page.evaluate("() => __winnow.S.timeline.timeRange") == {"enabled": False, "start": "", "end": ""}
    # And ✕ on the chip clears too.
    _apply(page, "2026-07-10 00:00:00", "")
    _wait_lines(page, ROWS[5:])
    _chip(page).locator(".tl-range-x").click()
    _wait_lines(page, ROWS)
    assert page.evaluate("() => __winnow.S.timeline.timeRange.start") == ""


def test_the_tables_timeframe_is_offered_not_followed(page, findings):
    """A range set to read one table would take findings off the Timeline
    unasked; the dialog offers to copy it in instead."""
    page.evaluate("""() => { __winnow.S.timeRange = { enabled: true, column: null,
                                                      start: '2026-07-09 09:00', end: '7/9/2026 9:30:00 AM' };
                       __winnow.updateTimeRangeButton(); }""")
    _open_timeline(page)
    s, e = _dialog(page)
    field = page.locator("#modalBody .dlg-field", has_text="From the tables' timeframe")
    assert "2026-07-09 09:00 → 7/9/2026 9:30:00 AM" in field.inner_text()
    field.locator(".btn", has_text="Fill range").click()
    assert (s.input_value(), e.input_value()) == ("2026-07-09 09:00:00", "2026-07-09 09:30:00")
    page.locator("#modalBody .btn", has_text="Apply").click()
    _wait_lines(page, ROWS[2:4])
