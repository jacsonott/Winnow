"""`Store.find_position` on a materialised view — and on a merge.

`rebuildView` keeps the cursor row across a filter change by identity:
`(source_id, rid)` in, position in the new view out, through
`/api/row_position`. An unfiltered, unsorted table answers that from the
source table itself (`pos = rid - 1`, pinned in `tests/test_views.py`);
everything else — any sort, any filter, and every merged view, which
always materialises with each row's own member `source_id` (invariant
#9) — answers from the view table, and nothing pinned that branch.
"""

from __future__ import annotations

A_ROWS = [["When", "Msg"], ["2024-01-01 10:00", "alpha beacon"], ["2024-01-01 11:00", "beta"]]
B_ROWS = [["When", "Msg"], ["2024-01-02 10:00", "gamma beacon"], ["2024-01-02 11:00", "delta"]]


def _mk(store, write_csv):
    a = store.ingest_csv(write_csv(A_ROWS, "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(B_ROWS, "b.csv"), name="b", build_fts=False)["id"]
    return a, b, store.create_merge("both", [a, b])["id"]


def _positions(store, view):
    """(source_id, rid) -> pos for every row, as the grid's page cache
    holds them — the same fields the frontend anchors on."""
    rows = store.fetch_rows(view["view_id"], 0, view["row_count"])["rows"]
    return {(r["source_id"], r["rid"]): r["pos"] for r in rows}


def test_every_row_of_a_sorted_merge_resolves_to_where_it_sits(store, write_csv):
    a, b, mid = _mk(store, write_csv)
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": [{"column": "When", "dir": "desc"}]})
    assert v["row_count"] == 4
    rows = _positions(store, v)
    assert {sid for sid, _ in rows} == {a, b}
    for (sid, rid), pos in rows.items():
        assert store.find_position(v["view_id"], sid, rid) == pos, (sid, rid)
    # Same rid, other member: a different row, at a different place.
    assert store.find_position(v["view_id"], a, 1) != store.find_position(v["view_id"], b, 1)
    # The merge's own id is nobody's source_id, so there is nothing to find.
    assert store.find_position(v["view_id"], mid, 1) is None


def test_a_member_row_the_filter_dropped_is_not_found(store, write_csv):
    a, b, mid = _mk(store, write_csv)
    v = store.build_view(mid, {"source_id": mid, "sort": [],
                               "filters": [{"column": "Msg", "op": "contains", "value": "beacon"}]})
    assert v["row_count"] == 2
    assert store.find_position(v["view_id"], a, 1) is not None
    assert store.find_position(v["view_id"], b, 1) is not None
    # In the table, not in this view — the answer that clears the cursor.
    assert store.find_position(v["view_id"], a, 2) is None
    assert store.find_position(v["view_id"], b, 2) is None


def test_a_plain_sorted_table_answers_the_same_way(store, write_csv):
    a, _, _ = _mk(store, write_csv)
    v = store.build_view(a, {"source_id": a, "filters": [], "sort": [{"column": "When", "dir": "desc"}]})
    assert store._views[v["view_id"]]["kind"] != "root_virtual"   # a sort materialises
    for (sid, rid), pos in _positions(store, v).items():
        assert store.find_position(v["view_id"], sid, rid) == pos
    assert store.find_position(v["view_id"], a, 9999) is None


def test_the_route_answers_for_a_merge_and_409s_for_a_dead_view(client, store, write_csv):
    a, b, mid = _mk(store, write_csv)
    v = client.post("/api/view", json={"source_id": mid, "sort": [{"column": "When", "dir": "asc"}]}).json()
    r = client.get("/api/row_position", params={"view_id": v["view_id"], "source_id": b, "rid": 2})
    assert r.status_code == 200 and r.json() == {"pos": 3}
    assert client.get("/api/row_position", params={"view_id": v["view_id"], "source_id": a, "rid": 99}).json() == {"pos": None}
    assert client.get("/api/row_position", params={"view_id": "view_nope", "source_id": a, "rid": 1}).status_code == 409
