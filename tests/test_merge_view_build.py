"""How build_view materialises a merge: what it reads, and the order it
lands in.

Clearing the filters on a 500k-row, five-member merge read every member's
whole table on each click (739 MB), where clearing them on a single table
reads its sort index. The merge's UNION ALL projected every column and was
sorted from outside, so SQLite ran it as a co-routine over full rows and
sorted the lot in a temp B-tree, never touching the per-member sort indexes
build_view itself asks for. It now projects only what it sorts on and puts
the ORDER BY on the compound, so the members stream in index order and
merge. The plan test pins the first half; the order tests pin that the
rewrite kept every position where it was, ties between members included."""

from __future__ import annotations

import re

import pytest


def _ingest(store, write_csv, rows, name, column_types=None):
    return store.ingest_csv(write_csv(rows, f"{name}.csv"), name=name, build_fts=False,
                            column_types=column_types)["id"]


def _merge_build_sql(store, mid, spec):
    """The SELECT build_view materialises the merge with. Any unfiltered
    view kept from an earlier build is let go first, or the build would be
    that table again and run no SELECT at all."""
    with store.lock, store.db:
        store._forget_merge_views(mid)
    seen = []
    store.db.set_trace_callback(seen.append)
    try:
        store.build_view(mid, spec)
    finally:
        store.db.set_trace_callback(None)
    insert = next(s for s in reversed(seen) if s.startswith("INSERT INTO v."))
    return insert[insert.index("SELECT"):]


def test_a_sorted_merge_reads_its_members_sort_indexes_not_their_rows(store, write_csv):
    """The clear-filters case: no filter, the default timestamp sort. Every
    member is read through its covering sort index, and nothing is sorted
    in a temp B-tree — the shape before this read every page of every
    member table, whatever indexes existed."""
    ids = []
    for k in range(3):
        rows = [["When", "Host", "Payload"]]
        rows += [[f"2024-01-0{k + 1} 10:{i:02d}:00", f"H{k}", "x" * 200] for i in range(40)]
        ids.append(_ingest(store, write_csv, rows, f"m{k}"))
    mid = store.create_merge("hosts", ids)["id"]
    spec = {"source_id": mid, "filters": [], "sort": [{"column": "When", "dir": "asc"}]}
    store.build_view(mid, spec)
    for sid in ids:
        assert store.wait_for_column_index(sid, "When", timeout=5, purpose="sort")

    sql = _merge_build_sql(store, mid, spec)
    plan = [r["detail"] for r in store.db.execute("EXPLAIN QUERY PLAN " + sql)]
    reads = [d for d in plan if d.startswith(("SCAN src_", "SEARCH src_"))]
    assert len(reads) == len(ids), plan
    assert all("COVERING INDEX" in d for d in reads), plan
    assert not any("TEMP B-TREE" in d for d in plan), plan
    # Only the member id, rid and the sort key travel; the old shape
    # dragged every column (here, the wide Payload) through the sort.
    assert '"Payload"' not in sql


A = [["User", "Count", "Msg"],
     ["bob", "10", "a1"], ["Alice", "9", "a2"], ["BOB", "x", "a3"], ["carol", "", "a4"], ["alice", "100", "a5"]]
B = [["User", "Count", "Msg"],
     ["Bob", "10", "b1"], ["alice", "9", "b2"], ["dave", "1e3", "b3"], ["bob", "-2", "b4"]]


# Count is forced numeric (the stray "x", "" and "1e3" would infer it as
# text), standing in for a column whose sampled rows all looked like
# numbers: _numeric_expr reads those three as NULL.
TYPES = ["text", "number", "text"]


def _ascii_lower(s):
    # SQLite's NOCASE folds ASCII only.
    return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in s)


def _num(s):
    return float(s) if re.fullmatch(r"-?\d+(\.\d+)?", s) else None


def _expected(members, sort):
    """(member, rid) in the order the merge view should hold them:
    each sort term in turn (NOCASE text, or numbers with non-numbers as
    NULL, NULL first ascending as SQLite has it), then rid, then member
    order."""
    rows = []
    for mi, (sid, data) in enumerate(members):
        header, body = data[0], data[1:]
        for rid, cells in enumerate(body, start=1):
            rows.append((mi, sid, rid, dict(zip(header, cells))))

    def keyed(direction, f):
        def k(r):
            v = f(r)
            # (is-not-null, value): NULLs first ascending, last descending.
            return (v is not None, v if v is not None else 0)
        return k, direction == "desc"

    keys = []
    for s in sort:
        col, d = s["column"], s.get("dir", "asc")
        if col == "__line__":
            keys.append(keyed(d, lambda r: r[2]))
        elif col not in rows[0][3]:
            continue   # an unknown column sorts nothing, as _compile_order has it
        elif col == "Count":
            keys.append(keyed(d, lambda r, c=col: _num(r[3][c])))
        else:
            keys.append(keyed(d, lambda r, c=col: _ascii_lower(r[3][c])))
    keys.append(keyed("asc", lambda r: r[2]))
    keys.append(keyed("asc", lambda r: r[0]))
    for k, rev in reversed(keys):   # stable sort, least significant first
        rows.sort(key=k, reverse=rev)
    return [(r[1], r[2]) for r in rows]


@pytest.mark.parametrize("sort", [
    [],
    [{"column": "User", "dir": "asc"}],
    [{"column": "User", "dir": "desc"}],
    [{"column": "Count", "dir": "asc"}],
    [{"column": "Count", "dir": "desc"}],
    [{"column": "User", "dir": "asc"}, {"column": "Count", "dir": "desc"}],
    [{"column": "__line__", "dir": "desc"}],
    [{"column": "__line__", "dir": "asc"}, {"column": "User", "dir": "asc"}],
    [{"column": "Gone", "dir": "asc"}],
])
def test_merge_view_order_with_ties_across_members(store, write_csv, sort):
    a = _ingest(store, write_csv, A, "a", TYPES)
    b = _ingest(store, write_csv, B, "b", TYPES)
    mid = store.create_merge("m", [a, b])["id"]
    assert {c["name"]: c["type"] for c in store.get_source(mid)["columns"]}["Count"] == "number"
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": sort})
    got = [(r["source_id"], r["rid"]) for r in store.fetch_rows(v["view_id"], 0, 100)["rows"]]
    assert got == _expected([(a, A), (b, B)], sort)


def test_filtered_merge_view_keeps_the_sort(store, write_csv):
    a = _ingest(store, write_csv, A, "a")
    b = _ingest(store, write_csv, B, "b")
    mid = store.create_merge("m", [a, b])["id"]
    sort = [{"column": "User", "dir": "desc"}]
    v = store.build_view(mid, {"source_id": mid, "sort": sort,
                               "filters": [{"column": "User", "op": "contains", "value": "b"}]})
    got = [(r["source_id"], r["rid"]) for r in store.fetch_rows(v["view_id"], 0, 100)["rows"]]
    want = [k for k in _expected([(a, A), (b, B)], sort)
            if "b" in _ascii_lower(dict(zip(A[0] if k[0] == a else B[0],
                                            (A if k[0] == a else B)[k[1]]))["User"])]
    assert got == want


def _v_tables(store):
    return {r[0] for r in store.db.execute("SELECT name FROM v.sqlite_master WHERE type='table'")
            if r[0].startswith("view_")}


def _inserts(store, fn):
    """fn's result, and how many view tables it wrote."""
    seen = []
    store.db.set_trace_callback(seen.append)
    try:
        out = fn()
    finally:
        store.db.set_trace_callback(None)
    return out, sum(1 for s in seen if s.startswith("INSERT INTO v."))


def _hosts(store, write_csv, n=3, rows=40):
    ids = []
    for k in range(n):
        data = [["When", "Host", "Payload"]]
        data += [[f"2024-01-0{(i % 9) + 1} 10:{i % 60:02d}:00", f"H{k}", f"p{i}"] for i in range(rows)]
        ids.append(_ingest(store, write_csv, data, f"m{k}"))
    return store.create_merge("hosts", ids)["id"], ids


SORTED = {"filters": [], "sort": [{"column": "When", "dir": "asc"}]}
FILTERED = {"filters": [{"column": "Host", "op": "equals", "value": "H1"}], "sort": [{"column": "When", "dir": "asc"}]}


def _keys(store, v):
    return [(r["source_id"], r["rid"]) for r in store.fetch_rows(v["view_id"], 0, 10_000)["rows"]]


@pytest.mark.parametrize("spec", [SORTED, {"filters": [], "sort": []},
                                  {"filters": [], "sort": [{"column": "Host", "dir": "desc"}]}])
def test_clearing_filters_on_a_merge_reuses_its_unfiltered_view(store, write_csv, spec):
    """A single table's unfiltered view is an index walk, or no table at
    all; a merge's is every member's rows merged into order and written
    again, each time the filters are cleared. The second time it is the
    table the first built — same rows, same order, nothing written."""
    mid, _ = _hosts(store, write_csv)
    first, wrote = _inserts(store, lambda: store.build_view(mid, {"source_id": mid, **spec}))
    want = _keys(store, first)
    assert wrote == 1 and len(want) == 120
    filtered = store.build_view(mid, {"source_id": mid, **FILTERED})
    assert first["view_id"] in _v_tables(store)   # kept while the filter is on
    again, wrote = _inserts(store, lambda: store.build_view(mid, {"source_id": mid, **spec}))
    assert wrote == 0
    assert again["view_id"] == first["view_id"] and again["row_count"] == 120
    assert _keys(store, again) == want
    # The filtered view went the usual way; only the kept one is left.
    assert filtered["view_id"] not in _v_tables(store)
    assert _v_tables(store) == {again["view_id"]}


def test_a_reused_merge_view_tags_groups_and_survives_its_children(store, write_csv):
    mid, ids = _hosts(store, write_csv)
    v = store.build_view(mid, {"source_id": mid, **SORTED})
    store.build_view(mid, {"source_id": mid, **FILTERED})
    v = store.build_view(mid, {"source_id": mid, **SORTED})
    tag = store.list_tags()[0]["id"]
    assert store.tag_view(v["view_id"], tag, True)["changed"] == 120
    group = store.expand_group(v["view_id"], "Host", "H2")
    assert group["row_count"] == 40
    store.close_view(group["view_id"])
    assert store.fetch_rows(v["view_id"], 0, 3)["rows"][0]["tags"] == [tag]
    # Evicted with its group by the next build, and still there to reuse.
    store.build_view(mid, {"source_id": mid, **FILTERED})
    v2 = store.build_view(mid, {"source_id": mid, **SORTED})
    assert v2["view_id"] == v["view_id"]
    assert sum(1 for r in store.fetch_rows(v2["view_id"], 0, 200)["rows"] if r["tags"] == [tag]) == 120


def test_a_kept_merge_view_is_not_reused_once_the_merge_changes(store, write_csv):
    mid, ids = _hosts(store, write_csv)
    first = store.build_view(mid, {"source_id": mid, **SORTED})
    store.update_merge(-mid, source_ids=ids[:2])
    assert first["view_id"] not in _v_tables(store)
    v, wrote = _inserts(store, lambda: store.build_view(mid, {"source_id": mid, **SORTED}))
    assert wrote == 1 and v["row_count"] == 80
    assert {k[0] for k in _keys(store, v)} == set(ids[:2])
    store.delete_merge(-mid)
    assert _v_tables(store) == set()


def test_at_most_two_unfiltered_views_are_kept_per_merge(store, write_csv):
    mid, _ = _hosts(store, write_csv)
    views = [store.build_view(mid, {"source_id": mid, "filters": [], "sort": [{"column": c, "dir": "asc"}]})
             for c in ("When", "Host", "Payload")]
    # The live one and the one before it; the oldest is gone.
    assert _v_tables(store) == {views[1]["view_id"], views[2]["view_id"]}
    _, wrote = _inserts(store, lambda: store.build_view(
        mid, {"source_id": mid, "filters": [], "sort": [{"column": "Host", "dir": "asc"}]}))
    assert wrote == 0
    _, wrote = _inserts(store, lambda: store.build_view(
        mid, {"source_id": mid, "filters": [], "sort": [{"column": "When", "dir": "asc"}]}))
    assert wrote == 1


def test_a_merge_sorted_on_a_derived_column_is_built_each_time(store, write_csv):
    """Its values are the analyst's, rewritten by a re-derive under the
    same SELECT, so the order a kept view holds could be stale."""
    mid, _ = _hosts(store, write_csv)
    res = store.add_derived_column(mid, "Num", "Payload", "regex_extract", {"pattern": r"p(\d+)"})
    for jid in res["job_ids"]:
        store.wait_for_ingest_job(jid, timeout=30)
    spec = {"source_id": mid, "filters": [], "sort": [{"column": "Num", "dir": "asc"}]}
    store.build_view(mid, spec)
    _, wrote = _inserts(store, lambda: store.build_view(mid, spec))
    assert wrote == 1
