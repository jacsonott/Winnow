"""time_histogram's answers kept by the rows a view holds
(Store._histogram_results): a sort, or a filter cleared back to the whole
table, is a new view of rows already counted, and used to be a whole pass
again — 2.2 s on a 2.1M-row event log, on every sort.

What matters here is the other half: an answer is only ever reused for
the same rows and the same question. A different filter, a tag written
since, a derived column still filling in or a merge given other tables
each has to count again."""

from __future__ import annotations

import contextlib

import pytest

ROWS = [["When", "User", "Host"]] + [
    [f"2026-03-14 {i // 3:02d}:{(i % 3) * 10:02d}:00", ["admin", "bob", "carol", ""][i % 4], f"H{i % 3}"]
    for i in range(24)
]


@pytest.fixture
def src(store, write_csv):
    return store.ingest_csv(write_csv(ROWS, "events.csv"), name="events", build_fts=False)["id"]


@pytest.fixture
def counted(store, monkeypatch):
    """How many aggregate passes over the rows each ask ran (any query
    naming the epoch alias `e`, which every pass does and nothing else)."""
    seen = []
    real = store._reader

    def note(sql):
        if " AS e" in sql:
            seen.append(sql)

    @contextlib.contextmanager
    def traced():
        with real() as ro:
            ro.set_trace_callback(note)
            try:
                yield ro
            finally:
                ro.set_trace_callback(None)

    monkeypatch.setattr(store, "_reader", traced)
    return seen


def _view(store, sid, **spec):
    return store.build_view(sid, {"source_id": sid, **spec})["view_id"]


SORT = {"sort": [{"column": "User", "dir": "asc"}]}
BOB = {"filters": [{"column": "User", "op": "equals", "value": "bob"}]}


def test_a_sort_of_the_whole_table_is_answered_without_a_pass(store, src, counted):
    want = store.time_histogram(_view(store, src), "When")
    assert counted
    counted.clear()
    assert store.time_histogram(_view(store, src, **SORT), "When") == want
    assert counted == []


def test_clearing_a_filter_gets_the_whole_table_back_without_a_pass(store, src, counted):
    want = store.time_histogram(_view(store, src), "When")
    assert store.time_histogram(_view(store, src, **BOB), "When")["total"] == 6
    counted.clear()
    assert store.time_histogram(_view(store, src), "When") == want
    assert counted == []


def test_a_filtered_view_sorted_keeps_its_answer(store, src, counted):
    want = store.time_histogram(_view(store, src, **BOB), "When")
    counted.clear()
    assert store.time_histogram(_view(store, src, **BOB, **SORT), "When") == want
    assert counted == []


def test_other_rows_of_the_same_size_count_again(store, src, counted):
    """bob and carol hold six rows each, at different times: a key that
    went by row count alone would hand carol bob's bars."""
    bob = store.time_histogram(_view(store, src, **BOB), "When")
    counted.clear()
    carol = store.time_histogram(_view(store, src, filters=[{"column": "User", "op": "equals", "value": "carol"}]), "When")
    assert counted and carol["total"] == bob["total"] == 6
    assert carol["buckets"] != bob["buckets"]


@pytest.mark.parametrize("kw", [
    {"max_buckets": 4},
    {"stack": "tags"},
    {"stack": "value", "split_column": "User", "split_values": ["admin"]},
    {"stack": "value", "split_column": "User", "split_values": ["bob"]},
    {"stack": "groups", "split_column": "User", "split_values": ["admin"]},
], ids=["bars", "tags", "admin", "bob", "groups"])
def test_another_question_about_the_same_rows_is_its_own_answer(store, src, counted, kw):
    store.time_histogram(_view(store, src), "When")
    store.time_histogram(_view(store, src), "When", stack="value", split_column="Host", split_values=["H1"])
    counted.clear()
    got = store.time_histogram(_view(store, src, **SORT), "When", **kw)
    assert counted, "answered from another question's answer"
    counted.clear()
    assert store.time_histogram(_view(store, src), "When", **kw) == got
    assert counted == []


def test_the_tag_split_counts_again_after_a_tag_is_written(store, src, counted):
    red = store.upsert_tag(None, "Red", "#ff0000", "1")
    view = _view(store, src)
    before = store.time_histogram(view, "When", stack="tags")
    store.set_tags(src, [1, 2, 3], red["id"], True)
    counted.clear()
    after = store.time_histogram(view, "When", stack="tags")
    assert counted and after != before
    assert sum(c[1] for _, c in after["stack"]["buckets"]) == 3
    # The plain chart does not depend on tags and keeps its answer.
    plain = store.time_histogram(view, "When")
    store.set_tags(src, [4], red["id"], True)
    counted.clear()
    assert store.time_histogram(_view(store, src, **SORT), "When") == plain
    assert counted == []


def test_an_undo_counts_again_too(store, src):
    red = store.upsert_tag(None, "Red", "#ff0000", "1")
    view = _view(store, src)
    before = store.time_histogram(view, "When", stack="tags")
    store.set_tags(src, [1, 2, 3], red["id"], True)
    store.time_histogram(view, "When", stack="tags")
    store.undo_last_tag_change()
    assert store.time_histogram(view, "When", stack="tags") == before


def test_a_derived_column_is_counted_every_time(store, src, counted):
    """Its values can still be filling in, so its answer is never kept —
    neither as the charted column nor as the split."""
    res = store.add_derived_column(src, "WhenISO", "When", "iso8601", {})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    res = store.add_derived_column(src, "HostNo", "Host", "regex_extract", {"pattern": r"H(\d)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    for column, kw in (("WhenISO", {}), ("When", {"stack": "value", "split_column": "HostNo", "split_values": ["0"]})):
        for _ in range(2):
            counted.clear()
            store.time_histogram(_view(store, src, **SORT), column, **kw)
            assert counted, (column, kw)


def test_a_merge_given_other_tables_counts_again(store, src, write_csv, counted):
    late = store.ingest_csv(write_csv([["When", "User", "Host"], ["2026-03-15 09:00:00", "x", "Z"]], "late.csv"),
                            name="late", build_fts=False)["id"]
    early = store.ingest_csv(write_csv([["When", "User", "Host"], ["2026-03-13 09:00:00", "x", "Z"]], "early.csv"),
                             name="early", build_fts=False)["id"]
    mid = store.create_merge("both", [src, late])["id"]
    first = store.time_histogram(_view(store, mid), "When")
    counted.clear()
    assert store.time_histogram(_view(store, mid, **SORT), "When") == first
    assert counted == []
    store.update_merge(-mid, source_ids=[src, early])
    again = store.time_histogram(_view(store, mid), "When")
    assert again["total"] == first["total"] and again["start"] == "2026-03-13 09:00:00"


def test_an_answer_handed_out_is_the_caller_s_to_change(store, src):
    view = _view(store, src)
    got = store.time_histogram(view, "When")
    got["buckets"].clear()
    again = store.time_histogram(view, "When")
    assert again["buckets"] and store.time_histogram(view, "When") == again
    again["buckets"].clear()
    assert store.time_histogram(view, "When")["buckets"]


def test_only_so_many_answers_are_kept(store, src):
    view = _view(store, src)
    for n in range(store.HISTOGRAM_ANSWERS_KEPT + 5):
        store.time_histogram(view, "When", max_buckets=n + 1)
    assert len(store._histogram_results) == store.HISTOGRAM_ANSWERS_KEPT
