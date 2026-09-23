"""The Timeline reads as a sentence, in the browser: a tagged EvtxECmd row
renders "Special privileges assigned  svc_backup · WKSTN-4471 · id 4672"
rather than the whole source row pipe-joined, and the raw row is still one
click away on the row itself.

The backend half is tests/test_timeline_summary.py. This exists for the
part no backend test can see: the summary is two styled spans and a button
inside a fixed-height virtualized row, which is exactly the shape of thing
that ships broken past a green suite.
"""

from __future__ import annotations

import pytest

from winnow import defaults

pytestmark = pytest.mark.ui

EVTX_COLUMNS = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]


def _evtx_csv(tmp_path):
    def row(**over):
        cells = dict.fromkeys(EVTX_COLUMNS, "")
        cells.update(over)
        return ",".join(cells[c] for c in EVTX_COLUMNS)

    path = tmp_path / "tl_evtx.csv"
    path.write_text(
        ",".join(EVTX_COLUMNS) + "\n"
        + row(RecordNumber="1", EventRecordId="100000", TimeCreated="2026-03-03 00:00:00",
              EventId="4672", Level="Information", Channel="Security",
              Provider="Microsoft-Windows-Security-Auditing", ProcessId="6868", ThreadId="2766",
              Computer="WKSTN-4471", MapDescription="Special privileges assigned",
              UserName="svc_backup") + "\n",
        encoding="utf-8")
    return path


@pytest.fixture
def tagged_evtx(page, tmp_path):
    """Imports an EvtxECmd-shaped table into the shared case, tags its one
    row through the UI, and takes the whole table away again afterwards
    (which takes its tags with it, so the next module's Timeline is as
    empty as it expects)."""
    before = set(page.evaluate("() => __winnow.S.sources.map((s) => s.id)"))
    page.evaluate("""([path, name]) => __winnow.post('/api/ingest/jobs/path',
         { path, name, kind: 'csv' }).then(() => __winnow.startJobsPoll())""",
                  [str(_evtx_csv(tmp_path)), "tl_evtx.csv"])
    page.wait_for_function(
        "() => __winnow.S.sources.some((s) => s.name === 'tl_evtx.csv')"
        " && !__winnow.ingestJobs.some((j) => j.status === 'running' || j.status === 'queued')",
        timeout=30_000)
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'tl_evtx.csv').id")
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=sid)
    page.wait_for_selector(".row")
    page.locator(".row").nth(0).locator(".cell").nth(1).click()
    page.keyboard.press("1")
    page.wait_for_timeout(250)
    yield sid
    page.evaluate("""async (id) => {
        await __winnow.api('/api/source/' + id, { method: 'DELETE' }).catch(() => {});
        __winnow.S.sourceId = null; __winnow.S.view = null;
        await __winnow.loadSources();
      }""", sid)


# The fixture row's timestamp, which is what addresses its row below. No
# other UI fixture uses this date (the shared case's ui.csv is all
# 2026-03-14), so it names exactly one row in the Timeline.
TS = "2026-03-03 00:00:00"


def _summary_row(page):
    """The fixture's row, addressed by its Timestamp cell.

    Deliberately not `has=.tl-lead`: "raw" removes `.tl-lead` from the row,
    and that is the click these tests are about. A Playwright locator is a
    query, not a handle — it re-runs on every use — so a lead-shaped filter
    would resolve to nothing on the line after the toggle and fail the
    assertion it exists to prove. Nor an element handle: the toggle calls
    renderTimelineRows(), which replaceChildren()s the whole window, so the
    row node itself is a different one afterwards. The Timestamp cell is
    the one part of the row neither the toggle nor the re-render changes.
    """
    page.locator("#tabTimeline").click()
    page.wait_for_selector("#timelineview:not([hidden])")
    row = page.locator(".timeline-row", has=page.locator(".tl-col-ts", has_text=TS))
    # Waiting on this row's own lead, not on any `.tl-lead` in the list:
    # the timeline is still building when the tab opens, and a wait that
    # any summarised row could satisfy would hand back a row that is not
    # the fixture's.
    row.locator(".tl-lead").wait_for(timeout=10_000)
    assert row.count() == 1, f"expected one timeline row at {TS}, got {row.count()}"
    return row


def test_the_row_leads_with_what_happened_not_the_raw_columns(page, tagged_evtx):
    row = _summary_row(page)
    assert row.locator(".tl-lead").inner_text() == "Special privileges assigned"
    detail = row.locator(".tl-detail").inner_text()
    assert "svc_backup" in detail and "WKSTN-4471" in detail and "id 4672" in detail

    body = row.locator(".tl-col-body").inner_text()
    assert " | " not in body, f"still the pipe-joined source row: {body!r}"
    # The timestamp is the column immediately to the left; saying it twice
    # is what made the old body unreadable.
    ts = row.locator(".tl-col-ts").inner_text().strip()
    assert ts and ts not in body

    # The raw row is not lost: it's the cell's tooltip on every row.
    assert " | " in (row.locator(".tl-col-body").get_attribute("title") or "")


def test_raw_swaps_the_row_in_place_and_back(page, tagged_evtx):
    row = _summary_row(page)
    assert row.locator(".tl-lead").count() == 1
    height = row.bounding_box()["height"]

    row.locator(".tl-raw-toggle").click()
    assert row.locator(".tl-raw").count() == 1
    raw = row.locator(".tl-raw").inner_text()
    assert " | " in raw and "4672" in raw
    assert row.locator(".tl-lead").count() == 0
    # A row that grew would paint over its neighbour: every row in this
    # list is one ROW_H tall and the virtualized window's offsets assume it.
    assert row.bounding_box()["height"] == height

    row.locator(".tl-raw-toggle").click()
    assert row.locator(".tl-lead").inner_text() == "Special privileges assigned"


def test_clicking_raw_does_not_jump_to_the_row_in_its_table(page, tagged_evtx):
    row = _summary_row(page)
    row.locator(".tl-raw-toggle").click()
    page.wait_for_timeout(200)
    assert page.locator("#timelineview").get_attribute("hidden") is None
    assert page.evaluate("() => __winnow.S.activeTab") == "timeline"
