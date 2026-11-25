"""The report, through the real dialog: on a merge of an EvtxECmd table and
an MFTECmd table, a coalesce of TimeCreated then Created0x10 — each column
on one table only — showed "Member table '…' has no column 'Created0x10'"
in the preview, and Add column failed with the same. Now the preview
shows both tables' times, the column is made, and it is a datetime column
on the merge (its header menu offers the timestamp formats)."""

from __future__ import annotations

import re
import time

import pytest

pytestmark = pytest.mark.ui


def _ingest(api, path, name):
    rec = api("/api/ingest/path", "POST", {"path": str(path), "name": name, "build_fts": False})
    assert "id" in rec, rec
    return rec["id"]


def test_coalesce_of_timestamp_columns_each_on_one_table(page, api, tmp_path, derive_pick, format_detected,
                                                         derive_preview):
    home = page.evaluate("() => __winnow.S.sourceId")
    ev = tmp_path / "dmp_evtx.csv"
    ev.write_text("TimeCreated,DmpEventId\n2026-03-14 13:02:11.4410372,4624\n"
                  "2026-03-14 13:09:30.5521840,7045\n", encoding="utf-8")
    mf = tmp_path / "dmp_mft.csv"
    mf.write_text("Created0x10,DmpFileName\n2026-03-14 13:06:03.2200000,stage.ps1\n", encoding="utf-8")
    a = _ingest(api, ev, "dmp_evtx.csv")
    b = _ingest(api, mf, "dmp_mft.csv")
    mid = api("/api/merges", "POST", {"name": "dmp merge", "source_ids": [a, b]})["id"]
    try:
        page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", mid)
        page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=mid)
        page.wait_for_selector('.hcell[data-col="TimeCreated"]')
        page.wait_for_selector(".row")

        page.evaluate("() => __winnow.openDerivedColumnModal('TimeCreated')")
        page.wait_for_selector("#modal:not([hidden])")
        format_detected()
        derive_pick("First non-empty value")
        page.locator(".derived-columns select.fb-groupby-add").select_option("Created0x10")
        page.locator(".derived-name").fill("DmpWhen")
        # The preview answers with rows from both tables, not the refusal.
        text = derive_preview()
        assert "has no column" not in text, text
        outs = page.locator(".derived-preview-row .derived-out").all_inner_texts()
        assert "2026-03-14 13:02:11.4410372" in outs and "2026-03-14 13:06:03.2200000" in outs, outs

        page.click("#modalBody .btn:has-text('Add column')")
        page.wait_for_selector("#modal", state="hidden")
        # The backfill is a job; polled from Python (page.wait_for_function
        # does not await a promise predicate — see test_merge_flow.py).
        deadline = time.monotonic() + 25
        col = None
        while time.monotonic() < deadline:
            col = next((d for d in api(f"/api/derived?source_id={mid}") if d["name"] == "DmpWhen"), None)
            if col and col["status"] == "ready":
                break
            time.sleep(0.2)
        assert col and col["status"] == "ready", col
        page.wait_for_function(
            "() => __winnow.S.columns.some((c) => c.name === 'DmpWhen' && c.derived"
            " && c.derived_status === 'ready' && c.type === 'datetime')", timeout=15_000)
        # A datetime column's header menu leads with the timestamp formats.
        page.locator('.hcell[data-col="DmpWhen"]').scroll_into_view_if_needed()
        page.locator('.hcell[data-col="DmpWhen"]').click(button="right")
        page.wait_for_selector(".menu .menu-item")
        items = page.locator(".menu .menu-item").all_inner_texts()
        assert any(re.fullmatch(r"\W*As stored", i.strip()) for i in items), items
        page.keyboard.press("Escape")
    finally:
        api(f"/api/merges/{-mid}", "DELETE")
        for sid in (a, b):
            api(f"/api/source/{sid}", "DELETE")
        page.evaluate("""(home) => (async () => {
          __winnow.S.sourceId = null;
          await __winnow.loadSources();
          await __winnow.openSource(home);
        })()""", home)
        page.wait_for_selector(".row")
