"""Adding a Row as JSON column through the modal: every other column
pre-filled as chips, the Skip empty cells checkbox, a live preview, and
the column arriving in the grid as one-line JSON."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui


def test_row_as_json_prefills_every_column_and_lands_in_the_grid(page, format_detected, derive_pick):
    page.evaluate("() => __winnow.openDerivedColumnModal('Host')")
    page.wait_for_selector("#modal:not([hidden])")
    format_detected()
    derive_pick("Row as JSON")
    assert page.locator(".derived-name").input_value() == "Row as JSON"
    chips = [c.replace("✕", "").strip() for c in page.locator(".derived-columns .fb-groupby-chip").all_inner_texts()]
    others = page.evaluate("() => __winnow.S.columns.map((c) => c.name).filter((n) => n !== 'Host')")
    assert "ExtremelyLongColumnHeaderName" in others
    assert chips == others   # every column but Host, in the table's order
    cb = page.locator("#modalBody .derived-param-bool input[type=checkbox]")
    assert cb.count() == 1 and not cb.is_checked()
    # The preview shows a JSON object with the parse column first
    page.wait_for_function("() => /\\{\"Host\":\"H\\d\",\"Timestamp\"/.test(document.querySelector('#modalBody').textContent)")
    # Drop one column, tick skip-empty, add the column
    page.locator(".derived-columns .fb-groupby-chip", has_text="ExtremelyLongColumnHeaderName").locator("button").click()
    cb.check()
    page.locator("#modalBody button", has_text="Add column").click()
    page.wait_for_function("() => __winnow.S.columns.some((c) => c.name === 'Row as JSON')", timeout=30_000)
    try:
        _assert_grid_value(page, ["Host"] + [c for c in others if c != "ExtremelyLongColumnHeaderName"])
    finally:
        # The shared table's header set must be left as found: a derived
        # column that stays changes it, and the saved-filter ring test
        # that runs later then finds no matching filter.
        page.evaluate("""() => fetch('/api/derived?source_id=' + __winnow.S.sourceId,
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())
          .then((defs) => Promise.all(defs.filter((x) => x.name === 'Row as JSON').map((x) =>
            fetch('/api/derived/' + x.id, { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } }))))""")
        page.evaluate("() => __winnow.loadSources()")
        page.wait_for_function("() => !__winnow.S.columns.some((c) => c.name === 'Row as JSON')", timeout=15_000)


def _assert_grid_value(page, keys):
    # The backfill is a job: poll the first row through the rows API (cells
    # are in S.columns order) until the new column has its value.
    cell = ""
    for _ in range(100):
        cell = page.evaluate("async () => { const i = __winnow.S.columns.findIndex((c) => c.name === 'Row as JSON'); "
                             "if (i < 0 || !__winnow.S.view) return ''; "
                             "const r = await __winnow.api(`/api/rows?view_id=${__winnow.S.view.view_id}&start=0&count=1`); "
                             "return (r.rows[0] && r.rows[0].cells[i]) || ''; }")
        if cell:
            break
        page.wait_for_timeout(200)
    obj = json.loads(cell)
    assert list(obj) == keys
    assert obj["EventId"] in (4624, 4625, 4688, 1) and isinstance(obj["EventId"], int)
    assert obj["Host"].startswith("H")


# A header no other module ingests, so nothing else lands in this merge.
RJ_HEADER = "RjWhen,RjUser"


def test_row_as_json_on_a_merge_includes_from_table(page, api, tmp_path, format_detected, derive_pick):
    """On a merge with a From table column, the dialog lists it among the
    columns like every other, and Add column used to refuse it ("'From
    table' names the table each row came from… can't be derived from").
    Each row's JSON now names its table the way the merge does — the
    nickname where the table has one."""
    home = page.evaluate("() => __winnow.S.sourceId")
    ids, merge_id = [], None
    try:
        for n, user in enumerate(("alice", "bob")):
            f = tmp_path / f"rj_{n}.csv"
            f.write_text(f"{RJ_HEADER}\n2026-03-1{n} 09:00:00,{user}\n", encoding="utf-8")
            rec = api("/api/ingest/path", "POST", {"path": str(f), "name": f"rj_{n}.csv", "build_fts": False})
            ids.append(rec["id"])
        api(f"/api/source/{ids[1]}/nickname", "POST", {"nickname": "RJ DC01"})
        merge_id = api("/api/merges", "POST", {"name": "rj merge", "source_ids": ids,
                                               "source_column": True})["id"]
        page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", merge_id)
        page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view", arg=merge_id)

        page.evaluate("() => __winnow.openDerivedColumnModal('RjUser')")
        page.wait_for_selector("#modal:not([hidden])")
        format_detected()
        derive_pick("Row as JSON")
        chips = [c.replace("✕", "").strip()
                 for c in page.locator(".derived-columns .fb-groupby-chip").all_inner_texts()]
        assert chips == ["From table", "RjWhen"]
        # Nothing is drawn for the hidden param that records it.
        assert page.locator('#modalBody [data-param="table_column"]').count() == 0
        page.wait_for_function("() => document.querySelector('#modalBody').textContent"
                               ".includes('{\"RjUser\":\"bob\",\"From table\":\"RJ DC01\"')")
        page.locator("#modalBody button", has_text="Add column").click()
        page.wait_for_function("() => __winnow.S.columns.some((c) => c.name === 'Row as JSON')", timeout=30_000)
        cells = []
        for _ in range(100):
            cells = page.evaluate("""async () => {
              const i = __winnow.S.columns.findIndex((c) => c.name === 'Row as JSON');
              if (i < 0 || !__winnow.S.view) return [];
              const r = await __winnow.api(`/api/rows?view_id=${__winnow.S.view.view_id}&start=0&count=2`);
              return r.rows.map((row) => row.cells[i] || ''); }""")
            if len(cells) == 2 and all(cells):
                break
            page.wait_for_timeout(200)
        assert sorted(json.loads(c)["From table"] for c in cells) == ["RJ DC01", "rj_0.csv"]
        assert all(list(json.loads(c)) == ["RjUser", "From table", "RjWhen"] for c in cells)
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
