"""The report, in the grid: two tables merged, one storing its times as ISO
with a Z ("2026-03-14T13:02:11Z") and the other as a US date with a
12-hour clock ("3/14/2026 1:06:03 PM"). Both timestamp columns show in the
case's format, alike. First non-empty value over the two came out as a
text column showing each table's times as stored, two shapes in one
column, sorted as text (the US time last). Now it is a datetime column:
each row shows the same time its own table's column shows, and sorting
by it interleaves the two tables by time."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui

# Every painted row's text under each of `names`, top to bottom, read off
# the DOM. null for a row not painted from a fetched page yet.
_ROWS = """(names) => {
  const cols = __winnow.visibleCols();
  const rows = [...document.querySelectorAll('#rows .row:not(.pending)')]
    .sort((a, b) => +a.dataset.pos - +b.dataset.pos);
  return rows.map((row) => names.map((n) => {
    const cell = row.querySelector(`.cell[data-col="${cols.indexOf(n)}"]`);
    return cell ? cell.textContent : null;
  }));
}"""


def _ingest(api, path, name):
    rec = api("/api/ingest/path", "POST", {"path": str(path), "name": name, "build_fts": False})
    assert "id" in rec, rec
    return rec["id"]


def test_coalesced_times_show_like_their_own_columns(page, api, tmp_path, derive_pick, format_detected,
                                                     derive_preview):
    home = page.evaluate("() => __winnow.S.sourceId")
    iso = tmp_path / "cts_iso.csv"
    iso.write_text("CtsTimestamp,CtsEvent\n2026-03-14T13:02:11Z,logon\n2026-03-14T13:09:30.552Z,service\n",
                   encoding="utf-8")
    us = tmp_path / "cts_us.csv"
    us.write_text("CtsLogged,CtsFile\n3/14/2026 1:06:03 PM,stage.ps1\n", encoding="utf-8")
    a = _ingest(api, iso, "cts_iso.csv")
    b = _ingest(api, us, "cts_us.csv")
    mid = api("/api/merges", "POST", {"name": "cts merge", "source_ids": [a, b]})["id"]
    try:
        page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", mid)
        page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=mid)
        page.wait_for_selector('.hcell[data-col="CtsTimestamp"]')
        page.wait_for_selector(".row")
        assert page.evaluate("() => __winnow.tsFormatFor('CtsTimestamp')") != "raw"

        page.evaluate("() => __winnow.openDerivedColumnModal('CtsTimestamp')")
        page.wait_for_selector("#modal:not([hidden])")
        format_detected()
        derive_pick("First non-empty value")
        page.locator(".derived-columns select.fb-groupby-add").select_option("CtsLogged")
        page.locator(".derived-name").fill("CtsWhen")
        derive_preview()
        # The preview already shows one shape: the US time read as 13:06.
        outs = page.locator(".derived-preview-row .derived-out").all_inner_texts()
        assert "2026-03-14 13:06:03" in outs and "2026-03-14 13:02:11" in outs, outs

        page.click("#modalBody .btn:has-text('Add column')")
        page.wait_for_selector("#modal", state="hidden")
        # The backfill is a job; polled from Python (page.wait_for_function
        # does not await a promise predicate — see test_merge_flow.py).
        deadline = time.monotonic() + 25
        col = None
        while time.monotonic() < deadline:
            col = next((d for d in api(f"/api/derived?source_id={mid}") if d["name"] == "CtsWhen"), None)
            if col and col["status"] == "ready":
                break
            time.sleep(0.2)
        assert col and col["status"] == "ready", col
        page.wait_for_function(
            "() => __winnow.S.columns.some((c) => c.name === 'CtsWhen' && c.derived"
            " && c.derived_status === 'ready' && c.type === 'datetime')", timeout=15_000)

        page.evaluate("() => { __winnow.S.sort = [{ column: 'CtsWhen', dir: 'asc' }];"
                      " return __winnow.rebuildView(); }")
        names = ["CtsWhen", "CtsTimestamp", "CtsLogged"]
        page.wait_for_function(
            f"(names) => {{ const r = ({_ROWS})(names);"
            " return r.length === 3 && r.every((x) => x.every((v) => v !== null)); }",
            arg=names, timeout=15_000)
        rows = page.evaluate(f"(names) => ({_ROWS})(names)", names)
        # Each row shows the time its own table's column shows, in the
        # same format — the US row too, which used to show as stored.
        for when, stamp, logged in rows:
            assert when == (stamp or logged), rows
        # And in time order across both tables: the US row in the middle.
        assert [r[2] != "" for r in rows] == [False, True, False], rows
        assert [r[0] for r in rows] == sorted(r[0] for r in rows), rows
    finally:
        page.evaluate("() => { __winnow.S.sort = []; }")
        api(f"/api/merges/{-mid}", "DELETE")
        for sid in (a, b):
            api(f"/api/source/{sid}", "DELETE")
        page.evaluate("""(home) => (async () => {
          __winnow.S.sourceId = null;
          await __winnow.loadSources();
          await __winnow.openSource(home);
        })()""", home)
        page.wait_for_selector(".row")
