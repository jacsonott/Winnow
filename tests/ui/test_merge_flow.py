"""Creating a merge through the UI — the builder modal groups sources by
matching columns and 'Create merge' opens the merged table. The merge
*engine* is tested to death (test_merge_parity et al); the modal flow an
analyst actually clicks through had nothing."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

HEADER = "Timestamp,EventId,Host,ExtremelyLongColumnHeaderName,CommandLine"


def test_merge_builder_creates_and_opens_a_merge(page, tmp_path):
    # A second source with the same columns, imported by path like the
    # import modal would.
    twin = tmp_path / "twin.csv"
    twin.write_text(HEADER + "\n2026-03-15 09:00:00,4624,H9,v,cmd.exe\n", encoding="utf-8")
    before = page.evaluate("() => __winnow.S.sources.length")
    page.evaluate(
        """(path) => __winnow.post('/api/ingest/jobs/path',
             { path, name: 'twin.csv', kind: 'csv' })
           .then(() => __winnow.startJobsPoll())""",
        str(twin))
    # Wait for the twin BY NAME, not just for a +1 count: on a loaded CI
    # runner a stale background job from an earlier test can satisfy the
    # count while the twin itself isn't in S.sources yet, and the merge
    # builder would then find no eligible pair and render no button.
    page.wait_for_function(
        """() => __winnow.loadSources().then(() =>
             __winnow.S.sources.some((s) => s.name === 'twin.csv'))""",
        timeout=20_000)

    merge_id = None
    try:
        page.evaluate("() => __winnow.openMergeBuilder()")
        page.wait_for_selector("#modal:not([hidden])")
        # The eligible group (ui_csv + twin, same columns) must have
        # rendered its checkboxes; assert it rather than let a missing
        # "Create merge" button time out with no explanation.
        page.wait_for_selector("#modal input[type=checkbox]", timeout=10_000)
        boxes = page.locator("#modal input[type=checkbox]")
        for i in range(boxes.count()):
            boxes.nth(i).check()
        create = page.locator("#modal button", has_text="Create merge")
        assert create.count() == 1, "the merge builder offered no eligible source group"
        create.click()

        page.wait_for_selector(".tab-merge", timeout=10_000)
        merge = page.evaluate(
            "() => __winnow.S.sources.find((s) => s.is_merge)")
        assert merge, "merge missing from the source list"
        merge_id = merge["id"]
        assert merge_id < 0
        # The merged table is what's open now, with both members' rows.
        page.wait_for_function(
            "(id) => __winnow.S.sourceId === id", arg=merge_id, timeout=10_000)
    finally:
        # Leave the shared case exactly as found: merge and twin gone.
        page.evaluate(
            """([mid, n]) => (async () => {
                 if (mid) await __winnow.api('/api/merges/' + (-mid), { method: 'DELETE' });
                 const twin = __winnow.S.sources.find((s) => s.name === 'twin.csv');
                 if (twin) await __winnow.api('/api/source/' + twin.id, { method: 'DELETE' });
                 await __winnow.loadSources();
                 const first = __winnow.S.sources.find((s) => !s.is_merge);
                 if (first) __winnow.openSource(first.id);
               })()""",
            [merge_id, before])
        page.wait_for_function(
            "(n) => __winnow.S.sources.length === n", arg=before, timeout=10_000)
        page.wait_for_selector(".row")
