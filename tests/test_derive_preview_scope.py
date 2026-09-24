"""Which rows the derive preview samples.

The preview existed to answer "will this operation read my data" before
committing to a backfill over millions of rows, and it sampled the first
200 non-empty values off the source table — the head of the file, which
is the one region a triage filter exists to escape. So it could report
that all 200 sampled values parse while every row the analyst had on
screen failed, and the analyst would find out after the backfill.

Two things beyond the scoping are pinned here because both are silent:
the sample's ORDER BY, which stops the planner quietly changing WHICH
rows are sampled once a column index exists, and the merge case, where
the source-scoped path only ever read the first member.
"""

from __future__ import annotations

import pytest

HEADERS = {"X-Timeline-Lite-Client": "1"}

# The head of the file parses; everything below it does not. A preview
# that only ever reads the head calls this column clean.
ROWS = [["When", "Host"]]
ROWS += [[f"2024-01-0{(i % 9) + 1} 10:00:00", "early"] for i in range(20)]
ROWS += [[f"not-a-timestamp-{i}", "late"] for i in range(20)]


@pytest.fixture
def seeded(store, write_csv):
    return store.ingest_csv(write_csv(ROWS, "mixed.csv"), build_fts=False)["id"]


def test_the_whole_table_sample_still_reads_the_head(store, seeded):
    """Unchanged behaviour, stated so the contrast below is real."""
    res = store.preview_derived(seeded, "When", "iso8601")
    assert res["sampled"] == 40
    assert res["failures"] == 20


def test_a_filtered_view_is_sampled_instead(store, seeded):
    """The point of the whole thing: the analyst filtered to the rows they
    care about, and the verdict is now about those rows."""
    view = store.build_view(seeded, {"filters": [{"column": "Host", "op": "equals", "value": "late"}], "sort": []})
    res = store.preview_derived(seeded, "When", "iso8601", view_id=view["view_id"])
    assert res["sampled"] == 20, res
    assert res["failures"] == 20, "every row in this view is unparseable and the preview must say so"

    clean = store.build_view(seeded, {"filters": [{"column": "Host", "op": "equals", "value": "early"}], "sort": []})
    ok = store.preview_derived(seeded, "When", "iso8601", view_id=clean["view_id"])
    assert ok["sampled"] == 20 and ok["failures"] == 0, ok


def test_an_unfiltered_view_matches_the_table(store, seeded):
    """An unfiltered, unsorted view is `root_virtual` — it has no view
    table at all, so the scoped path has to fall back to reading the
    source directly rather than joining something that was never made."""
    view = store.build_view(seeded, {})
    assert store.preview_derived(seeded, "When", "iso8601", view_id=view["view_id"]) \
        == store.preview_derived(seeded, "When", "iso8601")


def test_an_expired_view_is_a_409_not_a_404(store, seeded, monkeypatch):
    """404 would read as "no such column" and send the analyst looking for
    the wrong thing; the client retries a 409 against the table."""
    from fastapi.testclient import TestClient
    import server as server_mod

    monkeypatch.setattr(server_mod, "store", lambda: store)
    c = TestClient(server_mod.app)
    r = c.post("/api/derived/preview", headers=HEADERS, json={
        "source_id": seeded, "column": "When", "op_id": "iso8601", "view_id": "view_nope"})
    assert r.status_code == 409, r.text


def test_the_sample_is_the_head_even_once_an_index_exists(store, write_csv):
    """Without an ORDER BY the sample is a bare SCAN and the head of the
    file is the head by accident. A background column index — which any
    equals filter builds, unprompted — turns the same statement into a
    covering-index range scan that returns the lexicographically SMALLEST
    values instead. The sample an analyst previewed against would then
    differ between one open of a case and the next, with nothing on
    screen to explain it.

    Sized and shaped to make the planner actually switch: at 40 rows it
    keeps the scan and this passes either way, which is how a smaller
    version of this test sat here proving nothing. File order is the
    z-rows; lexicographic order is the a-rows."""
    rows = [["Val", "Host"]]
    rows += [[f"zzz-{i:05d}", "x"] for i in range(1500)]
    rows += [[f"aaa-{i:05d}", "x"] for i in range(1500)]
    sid = store.ingest_csv(write_csv(rows, "ordered.csv"), build_fts=False)["id"]
    src = store._source_lite(sid)
    assert store._sample_column(src, "Val", 3) == ["zzz-00000", "zzz-00001", "zzz-00002"]

    store._ensure_column_index_building(sid, "Val")
    store.wait_for_column_index(sid, "Val", timeout=30)
    assert store._sample_column(src, "Val", 3) == ["zzz-00000", "zzz-00001", "zzz-00002"], \
        "the index changed which rows the preview samples"


def test_a_merged_view_samples_every_member(store, write_csv):
    """The source-scoped path previews a merge against its first member
    only. A view over a merge carries each row's own source_id, so
    scoping to it reads all of them — which is what invariant #9 asks for
    and what an analyst merging two tools' output needs."""
    good = [["When", "Host"]] + [["2024-01-01 10:00:00", "a"] for _ in range(10)]
    bad = [["When", "Host"]] + [[f"nope-{i}", "b"] for i in range(10)]
    a = store.ingest_csv(write_csv(good, "good.csv"), build_fts=False)["id"]
    b = store.ingest_csv(write_csv(bad, "bad.csv"), build_fts=False)["id"]
    merge = store.create_merge("both", [a, b])

    # Member 0 alone looks clean, which is exactly the misleading answer.
    assert store.preview_derived(merge["id"], "When", "iso8601")["failures"] == 0

    view = store.build_view(merge["id"], {})
    both = store.preview_derived(merge["id"], "When", "iso8601", view_id=view["view_id"])
    assert both["sampled"] == 20, both
    assert both["failures"] == 10, "the second member's rows were never sampled"
