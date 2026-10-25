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
    """The SELECT build_view materialises the merge with."""
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
