"""The report, in the app: a column derived on one table was missing from
the merge it was in. The merge showed a derived column only when every
table had one of that name, while a file column only some tables have was
shown, empty in the others' rows. Now the derived column is there too: in
the merge builder's Columns tab (marked DERIVED, in 1 of 2 tables), in the
merged grid with its ƒ and 1/2 marks and its values on its own table's
rows, and Remove derived column… on the merge names the one table it
comes off."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui

# A header no other module ingests, so the merge builder's group for it
# holds exactly the tables this module imports.
HEADER = "PdWhen,PdWho,PdNote"


def _ingest(api, path, name):
    rec = api("/api/ingest/path", "POST", {"path": str(path), "name": name, "build_fts": False})
    assert "id" in rec, rec
    return rec["id"]


def _wait_ready(api, sid, timeout=25.0):
    """Polled from Python: page.wait_for_function does not await a
    promise predicate (see test_merge_flow.py)."""
    deadline = time.monotonic() + timeout
    defs = []
    while time.monotonic() < deadline:
        defs = api(f"/api/derived?source_id={sid}")
        if defs and all(d["status"] == "ready" for d in defs):
            return
        time.sleep(0.2)
    pytest.fail(f"derived columns on {sid} never finished: {defs}")


def test_a_column_derived_on_one_table_is_in_its_merge(page, api, tmp_path):
    home = page.evaluate("() => __winnow.S.sourceId")
    ids = []
    for n, user in enumerate(("alice", "carol")):
        f = tmp_path / f"pd_{n}.csv"
        f.write_text(f"{HEADER}\n2026-04-1{n} 09:00:00,u={user},x\n", encoding="utf-8")
        ids.append(_ingest(api, f, f"pd_{n}.csv"))
    a, b = ids
    merge_id = None
    try:
        # Derived on the SECOND table only, so nothing leans on the first.
        api("/api/derived", "POST", {"source_id": b, "name": "PdUser", "input_column": "PdWho",
                                     "op_id": "regex_extract", "params": {"pattern": r"u=(\w+)"}})
        _wait_ready(api, b)
        page.evaluate("() => __winnow.loadSources()")
        page.wait_for_function(
            "(id) => (__winnow.S.sources.find((s) => s.id === id) || {columns: []})"
            ".columns.some((c) => c.derived)", arg=b)

        page.evaluate("() => __winnow.openMergeBuilder()")
        page.wait_for_selector("#modal:not([hidden])")
        for n in ("pd_0.csv", "pd_1.csv"):
            page.locator("#modal label", has_text=n).locator("input[type=checkbox]").check()
        # The Columns tab lists it, marked, in one of the two tables.
        row = page.locator("#modal .mb-mx tr", has=page.locator("td.cn", has_text="PdUser"))
        row.wait_for()
        assert row.locator("td.cn .mb-added").text_content() == "derived"
        assert row.locator("td.cov").inner_text() == "1/2"
        page.locator("#modal button", has_text="Create merge").click()
        page.wait_for_function("() => __winnow.S.sourceId < 0", timeout=10_000)
        merge_id = page.evaluate("() => __winnow.S.sourceId")

        # The merged grid shows it, with the derived and partial marks.
        head = page.locator('.hcell[data-col="PdUser"]')
        head.wait_for()
        assert head.locator(".hcell-derived").text_content() == "ƒ"
        assert head.locator(".hcell-partial").text_content() == "1/2"
        assert "pd_1.csv" in head.locator(".hcell-partial").get_attribute("title")
        page.wait_for_function("""() => {
          const i = __winnow.S.columns.findIndex((c) => c.name === 'PdUser');
          const vals = [0, 1].map((p) => (__winnow.rowAt(p) || {cells: []}).cells[i]);
          return i >= 0 && vals.includes('carol') && vals.includes(null);
        }""", timeout=15_000)

        # Remove on the merge names the one table that has it.
        head.click(button="right")
        page.wait_for_selector(".menu .menu-item")
        page.locator(".menu .menu-item", has_text="Remove derived column").click()
        page.wait_for_selector(".confirm-overlay")
        assert "from pd_1.csv, the one table in this merge that has it" in \
            page.locator(".confirm-message").inner_text()
        page.locator(".confirm-card .btn", has_text="Remove").click()
        page.wait_for_function(
            "() => !__winnow.S.columns.some((c) => c.name === 'PdUser')", timeout=10_000)
        assert api(f"/api/derived?source_id={b}") == []
    finally:
        if merge_id is not None:
            api(f"/api/merges/{-merge_id}", "DELETE")
        for sid in ids:
            api(f"/api/source/{sid}", "DELETE")
        page.evaluate("""(home) => (async () => {
          __winnow.S.sourceId = null;
          await __winnow.loadSources();
          await __winnow.openSource(home);
        })()""", home)
        page.wait_for_selector(".row")
