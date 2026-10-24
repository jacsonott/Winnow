"""A table wider than the Timeline's body can join, in the browser.

One tagged row in a ~500-column table used to fail the whole build with
"Expression tree is too large (maximum depth 1000)": the tab showed a toast
and no rows at all, from any table. Now that table's body keeps its first
columns (tests/test_timeline.py has the backend half), and the header says
so — without the note, the columns past the cap read as empty ones.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

NCOLS = 600
TS = "2026-03-02 00:00:00"  # no other UI fixture uses this date


@pytest.fixture
def tagged_wide(page, tmp_path, tag_written):
    """Imports a 600-column table into the shared case, tags its one row
    through the UI, and takes the table (and with it the tag) away again."""
    path = tmp_path / "tl_wide.csv"
    path.write_text(
        ",".join(["Timestamp"] + [f"c{i}" for i in range(1, NCOLS)]) + "\n"
        + ",".join([TS] + [f"v{i}" for i in range(1, NCOLS)]) + "\n",
        encoding="utf-8")
    page.evaluate("""([path, name]) => __winnow.post('/api/ingest/jobs/path',
         { path, name, kind: 'csv' }).then(() => __winnow.startJobsPoll())""",
                  [str(path), "tl_wide.csv"])
    page.wait_for_function(
        "() => __winnow.S.sources.some((s) => s.name === 'tl_wide.csv')"
        " && !__winnow.ingestJobs.some((j) => j.status === 'running' || j.status === 'queued')",
        timeout=30_000)
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'tl_wide.csv').id")
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=sid)
    page.wait_for_selector(".row")
    page.locator(".row").nth(0).locator(".cell").nth(1).click()
    with tag_written(page):
        page.keyboard.press("1")
    yield sid
    page.evaluate("""async (id) => {
        await __winnow.api('/api/source/' + id, { method: 'DELETE' }).catch(() => {});
        __winnow.S.sourceId = null; __winnow.S.view = null;
        await __winnow.loadSources();
      }""", sid)


def test_a_wide_table_is_on_the_timeline_and_the_header_says_it_was_trimmed(page, tagged_wide):
    page.locator("#tabTimeline").click()
    page.wait_for_selector("#timelineview:not([hidden])")
    # The symptom: this never appeared ("Could not build timeline" instead).
    row = page.locator(f"#timelineRows .timeline-row[data-ts^='{TS}']")
    row.wait_for(timeout=10_000)

    body = row.locator(".tl-col-body").get_attribute("title") or ""
    assert body.startswith(f"{TS} | v1 | v2 | "), body[:80]
    shown = len(body.split(" | "))
    assert 400 < shown < NCOLS

    stats = page.locator("#timelineStats").inner_text()
    assert "tl_wide.csv" in stats and f"{shown} of {NCOLS} columns" in stats, stats
