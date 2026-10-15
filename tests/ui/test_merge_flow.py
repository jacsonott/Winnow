"""Creating and editing a merge through the UI — the builder lists every
table with a fit chip, previews the merge, and 'Create merged table' opens
it. The merge *engine* is tested to death (test_merge_parity,
test_merge_mismatched et al); this is the flow an analyst clicks through."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui

HEADER = "Timestamp,EventId,Host,ExtremelyLongColumnHeaderName,CommandLine"


def _import_csv(page, path, name):
    page.evaluate(
        """([path, name]) => __winnow.post('/api/ingest/jobs/path',
             { path, name, kind: 'csv' }).then(() => __winnow.startJobsPoll())""",
        [str(path), name])


def _wait_for_source(page, name, timeout=25.0):
    """Polled from Python: page.wait_for_function does NOT await a promise
    predicate, so `() => loadSources().then(...)` passed instantly and the
    test raced the import — which is how this flaked on CI for months."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        names = page.evaluate(
            "() => __winnow.loadSources().then(() => __winnow.S.sources.map((s) => s.name))")
        if name in names:
            return
        time.sleep(0.25)
    pytest.fail(f"{name} never appeared in S.sources")


def _cleanup(page, merge_id, names, before):
    page.evaluate(
        """([mid, ns]) => (async () => {
             if (mid) await __winnow.api('/api/merges/' + (-mid), { method: 'DELETE' });
             for (const n of ns) {
               const s = __winnow.S.sources.find((x) => x.name === n);
               if (s) await __winnow.api('/api/source/' + s.id, { method: 'DELETE' });
             }
             __winnow.S.sourceId = null;
             await __winnow.loadSources();
           })()""",
        [merge_id, list(names)])
    page.wait_for_function("(n) => __winnow.S.sources.length === n", arg=before, timeout=15_000)
    page.wait_for_selector(".row")


def test_merge_builder_merges_mismatched_tables_and_edits_in_place(page, tmp_path):
    """Imports its own three tables rather than leaning on the shared
    fixture: anything an earlier test does to that one (a derived column is
    enough) would change what the builder shows. merge_c has an extra
    column, which the old builder hid it for; now it merges, with the
    column empty for the other two."""
    names = ("merge_a.csv", "merge_b.csv", "merge_c.csv")
    before = page.evaluate("() => __winnow.S.sources.length")
    for n in names:
        f = tmp_path / n
        extra = n == "merge_c.csv"
        f.write_text(HEADER + (",Extra" if extra else "")
                     + "\n2026-03-15 09:00:00,4624,H9,v,cmd.exe" + (",only-c" if extra else "") + "\n",
                     encoding="utf-8")
        _import_csv(page, f, n)
    for n in names:
        _wait_for_source(page, n)

    merge_id = None
    try:
        page.evaluate("() => __winnow.openMergeBuilder()")
        page.wait_for_selector("#modal .mb")
        for n in ("merge_a.csv", "merge_c.csv"):
            box = page.locator("#modal label.mb-trow", has_text=n).locator("input[type=checkbox]")
            assert box.count() == 1, f"the builder did not offer {n}"
            box.check()
        # The preview is the server's: Extra is named as only in merge_c.
        page.wait_for_selector(".mb-notes li:has-text('Extra is only in merge_c.csv')", timeout=10_000)
        assert page.locator(".mb-stat b").nth(2).text_content() == "7"  # 5 + Extra + From table
        create = page.locator("#modal button.mb-create")
        assert "Create merged table" in create.text_content()
        create.click()

        page.wait_for_selector(".tab-merge", timeout=10_000)
        merge = page.evaluate("() => __winnow.S.sources.find((s) => s.is_merge)")
        assert merge, "merge missing from the source list"
        merge_id = merge["id"]
        assert [c["name"] for c in merge["columns"]][:1] == ["From table"]
        assert "Extra" in [c["name"] for c in merge["columns"]]
        page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=merge_id, timeout=10_000)
        page.wait_for_selector(".hcell-partial", state="attached")

        # Edit it from the table menu: add merge_b, drop the source column.
        page.evaluate("(id) => __winnow.openTableMenu(id)", merge_id)
        page.locator("#modal button", has_text="Edit merge").click()
        page.wait_for_selector("#modal .mb")
        page.locator("#modal label.mb-trow", has_text="merge_b.csv").locator("input").check()
        page.locator("#modal .mb-check input").uncheck()
        save = page.locator("#modal button.mb-create")
        page.wait_for_function("() => !document.querySelector('#modal button.mb-create').disabled")
        assert "Save changes" in save.text_content()
        save.click()
        page.wait_for_function(
            """(id) => { const s = __winnow.S.sources.find((x) => x.id === id);
                         return s && s.member_source_ids.length === 3 && !s.source_column; }""",
            arg=merge_id, timeout=10_000)
    finally:
        _cleanup(page, merge_id, names, before)
