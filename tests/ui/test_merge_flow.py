"""Creating a merge through the UI — the builder modal groups sources by
matching columns and 'Create merge' opens the merged table. The merge
*engine* is tested to death (test_merge_parity et al); the modal flow an
analyst actually clicks through had nothing."""

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


def test_merge_builder_creates_and_opens_a_merge(page, tmp_path):
    """Imports BOTH of its own sources rather than pairing one with the
    shared fixture table. The builder groups by column set, so anything an
    earlier test does to that table — adding a derived column is enough to
    change its key — used to leave this test with no eligible pair and a
    checkbox wait that timed out with nothing to say."""
    names = ("merge_a.csv", "merge_b.csv")
    before = page.evaluate("() => __winnow.S.sources.length")
    for n in names:
        f = tmp_path / n
        f.write_text(HEADER + f"\n2026-03-15 09:00:00,4624,H9,v,cmd.exe\n", encoding="utf-8")
        _import_csv(page, f, n)
    for n in names:
        _wait_for_source(page, n)

    merge_id = None
    try:
        page.evaluate("() => __winnow.openMergeBuilder()")
        page.wait_for_selector("#modal:not([hidden])")
        # Check exactly OUR two, by their labels: the case may hold other
        # eligible groups, and a merge is only valid within one group.
        page.wait_for_selector("#modal input[type=checkbox]", timeout=10_000)
        for n in names:
            box = page.locator("#modal label", has_text=n).locator("input[type=checkbox]")
            assert box.count() == 1, f"the builder did not offer {n}"
            box.check()
        create = page.locator("#modal button", has_text="Create merge")
        assert create.count() == 1, "the merge builder offered no eligible source group"
        create.click()

        page.wait_for_selector(".tab-merge", timeout=10_000)
        merge = page.evaluate("() => __winnow.S.sources.find((s) => s.is_merge)")
        assert merge, "merge missing from the source list"
        merge_id = merge["id"]
        assert merge_id < 0
        page.wait_for_function(
            "(id) => __winnow.S.sourceId === id", arg=merge_id, timeout=10_000)
    finally:
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
