"""The Timeline read as a story: the day said once, at the first line of
each day; a Gap column with the time since the line above, and a rule
over a long one; a row's note in its line; and Fold repeats, which puts a
run of the same thing on one line that says how many, and opens it again.

The server half (which rows make a run, what never folds, the gap and the
day across a page boundary) is tests/test_timeline_story.py.
"""

from __future__ import annotations

import pytest

from winnow import defaults

pytestmark = pytest.mark.ui

EVTX_COLUMNS = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]

# No other UI fixture uses these days. Four failed logons in a row, the
# success two minutes on (with a note), then the next day, twice.
ROWS = [
    ("2026-06-11 23:10:00", "Failed logon"),
    ("2026-06-11 23:10:20", "Failed logon"),
    ("2026-06-11 23:11:05", "Failed logon"),
    ("2026-06-11 23:12:00", "Failed logon"),
    ("2026-06-11 23:14:17", "Successful logon"),
    ("2026-06-12 01:45:00", "Process created"),
    ("2026-06-12 03:00:00", "Process created"),
]
NOTE = "the one that worked"
FOLD_KEY = "winnow.timelineFold"


@pytest.fixture
def story(page, tmp_path, api):
    def row(i, ts, what):
        cells = dict.fromkeys(EVTX_COLUMNS, "")
        cells.update(RecordNumber=str(i), TimeCreated=ts, EventId="4625", Computer="WS07",
                     MapDescription=what, UserName="svc_backup")
        return ",".join(cells[c] for c in EVTX_COLUMNS)

    name = "WS07_tlstory.csv"
    path = tmp_path / name
    path.write_text(",".join(EVTX_COLUMNS) + "\n"
                    + "".join(row(i, *r) + "\n" for i, r in enumerate(ROWS, 1)), encoding="utf-8")
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
        api("/api/note", "POST", {"source_id": sid, "rid": 5, "note": NOTE})
        page.evaluate("() => __winnow.loadSources()")
        yield sid
    finally:
        api(f"/api/source/{sid}", "DELETE")
        page.evaluate("""async (key) => {
            localStorage.removeItem(key);
            __winnow.renderTimelineFoldButton();
            __winnow.S.sourceId = null; __winnow.S.view = null;
            Object.assign(__winnow.S.timeline, { sel: null, range: null, context: null, unfold: [] });
            await __winnow.loadSources();
          }""", FOLD_KEY)


def _open_timeline(page, lines=len(ROWS)):
    page.locator("#tabTimeline").click()
    page.wait_for_selector("#timelineview:not([hidden])")
    _wait_lines(page, lines)


def _wait_lines(page, n):
    page.wait_for_function("(n) => (__winnow.S.timeline.view || {}).row_count === n", arg=n, timeout=10_000)
    page.wait_for_function("(n) => document.querySelectorAll('#timelineRows .timeline-row:not(.pending)').length === n",
                           arg=n, timeout=10_000)


def _line(page, ts):
    return page.locator(f"#timelineRows .timeline-row[data-ts^='{ts}']")


def test_the_day_is_said_once_and_each_line_says_the_gap_above_it(page, story):
    _open_timeline(page)
    # The day at the first line of each day only, not on every line.
    assert page.locator("#timelineRows .tl-day").all_inner_texts() == ["Thu 11 Jun", "Fri 12 Jun"]
    assert _line(page, ROWS[5][0]).locator(".tl-day").inner_text() == "Fri 12 Jun"
    # Each line's time of day, and the full timestamp kept as the title.
    ts = _line(page, ROWS[1][0]).locator(".tl-col-ts")
    assert ts.inner_text().strip() == "23:10:20"
    assert ts.get_attribute("title") == ROWS[1][0]
    gaps = page.locator("#timelineRows .timeline-row .tl-col-gap").all_inner_texts()
    assert gaps == ["", "+20s", "+45s", "+55s", "+2m 17s", "+2h 30m", "+1h 15m"]
    # Half an hour or more is said louder, with a rule over the line; a
    # new day has a rule of its own.
    long = page.locator("#timelineRows .tl-col-gap.long")
    assert long.count() == 2
    assert "day-start" in _line(page, ROWS[5][0]).get_attribute("class")
    assert "gap" in _line(page, ROWS[6][0]).get_attribute("class").split()
    # The rules are inside the row: every line is still the height the
    # virtual list places them by, or the lines under a rule would drift.
    heights = page.evaluate(
        "() => [...document.querySelectorAll('#timelineRows .timeline-row')].map((r) => r.getBoundingClientRect().height)")
    assert len(set(heights)) == 1, heights


def test_a_note_is_read_in_its_line(page, story):
    _open_timeline(page)
    note = _line(page, ROWS[4][0]).locator(".tl-note")
    assert note.inner_text() == f"✎ {NOTE}"
    # After what happened, before who and where.
    order = _line(page, ROWS[4][0]).evaluate(
        "(r) => [...r.querySelector('.tl-col-body').children].map((c) => c.className)")
    assert order[:3] == ["tl-lead", "tl-note", "tl-detail"], order


def test_fold_repeats_puts_a_run_on_one_line_and_opens_it_again(page, story):
    _open_timeline(page)
    assert page.locator("#btnTimelineFold").get_attribute("aria-pressed") == "false"
    page.locator("#btnTimelineFold").click()
    _wait_lines(page, 4)
    assert page.locator("#btnTimelineFold").get_attribute("aria-pressed") == "true"
    assert page.locator("#timelineStats").inner_text() == "7 tagged rows in 4 lines"
    head = _line(page, ROWS[0][0])
    assert head.locator(".tl-fold").inner_text() == "×4"
    assert head.locator(".tl-span").inner_text() == "23:10:00 → 23:12:00 · over 2m"
    assert _line(page, ROWS[2][0]).count() == 0
    # The line after a run is measured from the run's last row.
    assert _line(page, ROWS[4][0]).locator(".tl-col-gap").inner_text() == "+2m 17s"

    # The pane says the line is a run, and whose tags and note it shows.
    head.locator(".tl-col-type").click()
    page.wait_for_function("() => /The first of 4 rows folded into this line/.test("
                           "document.getElementById('timelinePane').textContent)")
    assert "23:10:00 to 23:12:00" in page.locator("#timelinePane .tl-pane-hint").inner_text()

    # ×4 opens the run, a line per row, and its first line can fold it back.
    head.locator(".tl-fold").click()
    _wait_lines(page, len(ROWS))
    _line(page, ROWS[2][0]).wait_for()
    again = _line(page, ROWS[0][0]).locator(".tl-fold.open")
    assert again.inner_text() == "fold"
    assert page.locator("#timelineRows .tl-fold").count() == 1
    # A row picked inside the run is still picked once it folds away: the
    # run's line is.
    _line(page, ROWS[2][0]).locator(".tl-col-type").click()
    again.click()
    _wait_lines(page, 4)
    page.wait_for_function("(ts) => ((document.querySelector('#timelineRows .timeline-row.selected') || {})"
                           ".dataset || {}).ts === ts", arg=ROWS[0][0], timeout=10_000)

    # Remembered per browser, like Overview and Details.
    page.reload()
    page.wait_for_selector("#tabTimeline", state="visible")
    # Boot reopens the case's table after #tabTimeline shows; the Timeline
    # opened before it lands would be hidden again under the test.
    page.wait_for_function("() => window.__winnow && __winnow.S.view", timeout=30_000)
    _open_timeline(page, 4)
    assert page.locator("#btnTimelineFold").get_attribute("aria-pressed") == "true"
    page.locator("#btnTimelineFold").click()
    _wait_lines(page, len(ROWS))
    assert page.locator("#timelineRows .tl-fold").count() == 0
    assert page.locator("#timelineStats").inner_text() == "7 tagged rows"
