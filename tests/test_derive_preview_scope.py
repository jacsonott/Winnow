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
    scoped = store.preview_derived(seeded, "When", "iso8601", view_id=view["view_id"])
    whole = store.preview_derived(seeded, "When", "iso8601")
    assert {k: v for k, v in scoped.items() if k != "scope"} \
        == {k: v for k, v in whole.items() if k != "scope"}
    # Same rows, different label, both honest: the sample was taken for
    # the view the analyst named, and it happens to hold every row.
    assert scoped["scope"] == "view" and whole["scope"] == "table"


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
    and what an analyst merging two tools' output needs.

    Each member holds MORE rows than DETECT_SAMPLE on purpose. Unioning
    the members and capping the union is not the same thing as reading
    them all: UNION ALL runs in order, so an outer LIMIT drains the first
    member and never reaches the second, and this test passed at ten rows
    a member while the sample was 100% member 0. At 300 a member the
    budget is split per member, and a merge that is half unreadable has
    to say so."""
    good = [["When", "Host"]] + [["2024-01-01 10:00:00", "a"] for _ in range(300)]
    bad = [["When", "Host"]] + [[f"nope-{i}", "b"] for i in range(300)]
    a = store.ingest_csv(write_csv(good, "good.csv"), build_fts=False)["id"]
    b = store.ingest_csv(write_csv(bad, "bad.csv"), build_fts=False)["id"]
    merge = store.create_merge("both", [a, b])

    # Member 0 alone looks clean, which is exactly the misleading answer.
    assert store.preview_derived(merge["id"], "When", "iso8601")["failures"] == 0

    view = store.build_view(merge["id"], {})
    both = store.preview_derived(merge["id"], "When", "iso8601", view_id=view["view_id"])
    assert both["sampled"] == store.DETECT_SAMPLE, both
    assert both["failures"] == store.DETECT_SAMPLE // 2, \
        "half this merge is unreadable and the sample came off the first member only"


def test_the_view_sample_is_stable_once_a_column_index_exists(store, write_csv):
    """_sample_column pins its ORDER BY for a reason (the test above); the
    view-scoped sample needs the same pin and did not have it.

    A view that holds every row of its source reads the member tables
    directly rather than joining the view table — cheaper, and the same
    answer. That branch had no ORDER BY, so which rows it sampled was the
    planner's choice, and the planner changes its mind the moment a
    background column index appears: the sample flips from file order to
    the lexicographically smallest values, and an analyst reopening the
    case previews against different rows than they did yesterday with
    nothing on screen to say why.

    3,000 rows and a filter that matches all of them: big enough that the
    planner really does switch to the index, and `direct` because the view
    holds the whole source."""
    rows = [["Val", "Host"]]
    rows += [[f"zzz-{i:05d}", "x"] for i in range(1500)]
    rows += [[f"aaa-{i:05d}", "x"] for i in range(1500)]
    sid = store.ingest_csv(write_csv(rows, "ordered-view.csv"), build_fts=False)["id"]
    view = store.build_view(sid, {"filters": [{"column": "Host", "op": "equals", "value": "x"}],
                                  "sort": []})
    assert view["row_count"] == 3000, "the filter has to leave the whole source behind it"
    assert store._sample_column_in_view(view["view_id"], "Val", 3) == \
        ["zzz-00000", "zzz-00001", "zzz-00002"]

    store._ensure_column_index_building(sid, "Val")
    store.wait_for_column_index(sid, "Val", timeout=30)
    assert store._sample_column_in_view(view["view_id"], "Val", 3) == \
        ["zzz-00000", "zzz-00001", "zzz-00002"], \
        "the index changed which rows the view-scoped preview samples"


def test_a_column_named_for_an_expiry_is_still_a_missing_column(store, write_csv, monkeypatch):
    """404, not 409. The route used to tell an expired view from an
    unknown column by looking for "expired" in the message — but the
    unknown-column KeyError carries the column NAME, and column names are
    the analyst's data (invariant #5). A table whose columns are named
    after expiry dates therefore answered "that view was rebuilt" to a
    question about a column, and the client dutifully retried a request
    whose real problem was not going to change."""
    from fastapi.testclient import TestClient
    import server as server_mod

    rows = [["When", "expired_at"], ["2024-01-01 10:00:00", "2024-02-01 10:00:00"]]
    sid = store.ingest_csv(write_csv(rows, "expiry.csv"), build_fts=False)["id"]
    view = store.build_view(sid, {"filters": [{"column": "When", "op": "contains", "value": "2024"}],
                                  "sort": []})
    monkeypatch.setattr(server_mod, "store", lambda: store)
    c = TestClient(server_mod.app)
    for view_id in (None, view["view_id"]):
        r = c.post("/api/derived/preview", headers=HEADERS, json={
            "source_id": sid, "column": "expired_at_renamed", "op_id": "iso8601",
            "view_id": view_id})
        assert r.status_code == 404, (view_id, r.status_code, r.text)

    # The histogram route made the same message-based split, against the
    # same user data. Pinned here beside the preview because they share a
    # cause and would otherwise be re-broken one at a time: the strip asks
    # for a column that is not there, and a 409 would leave it waiting for
    # a view change that fixes nothing.
    r = c.get("/api/histogram", headers=HEADERS,
              params={"view_id": view["view_id"], "column": "expired_at_renamed"})
    assert r.status_code == 400, (r.status_code, r.text)


def test_a_multi_column_operation_reports_the_scope_it_actually_used(store, write_csv):
    """An operation that reads two columns of a row cannot be scoped by
    the one-column sample, so it is previewed against the table even when
    a view_id is passed — and the payload says `table`, because the modal
    writes "in this view" or "in the whole table" from that field. Saying
    nothing left the client labelling a whole-table verdict with the
    view's name, which is the exact claim this feature exists to stop."""
    rows = [["End", "Start", "Host"]]
    rows += [["2024-01-01 10:00:10", "2024-01-01 10:00:00", "early"] for _ in range(10)]
    rows += [[f"nope-{i}", "2024-01-01 10:00:00", "late"] for i in range(10)]
    sid = store.ingest_csv(write_csv(rows, "durations.csv"), build_fts=False)["id"]
    view = store.build_view(sid, {"filters": [{"column": "Host", "op": "equals", "value": "late"}],
                                  "sort": []})

    single = store.preview_derived(sid, "End", "iso8601", view_id=view["view_id"])
    assert single["scope"] == "view" and single["failures"] == 10, single

    pair = store.preview_derived(sid, "End", "duration_delta", {"other_column": "Start"},
                                 view_id=view["view_id"])
    # Every row of the view is unreadable; these numbers are the table's
    # head, which is why the label must not say "in this view".
    assert pair["scope"] == "table", pair
    assert pair["failures"] == 0, pair
