"""Store.view_keys / view_positions: the row ids behind a selection's
positions, and their positions in another view — the round trip that keeps
a selection across a sort or a filter."""

ROWS = [["A", "B"], ["x", "1"], ["y", "2"], ["z", "3"], ["x", "4"], ["y", "5"]]


def test_round_trip_through_a_sort_and_a_filter(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "r.csv"), name="r", build_fts=False)["id"]
    v0 = store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})    # root_virtual
    keys = store.view_keys(v0["view_id"], [0, 2, 4])
    assert keys == [[sid, 1], [sid, 3], [sid, 5]]
    v1 = store.build_view(sid, {"source_id": sid, "filters": [], "sort": [{"column": "B", "dir": "desc"}]})
    r = store.view_positions(v1["view_id"], keys)
    assert r == {"positions": [0, 2, 4], "missing": 0}     # rids 5,3,1 sit at 0,2,4 in the reversed view
    v2 = store.build_view(sid, {"source_id": sid, "filters": [{"column": "A", "op": "equals", "value": "x"}], "sort": []})
    r = store.view_positions(v2["view_id"], keys)
    assert r == {"positions": [0], "missing": 2}           # only rid 1 is an x
    assert store.view_keys(v2["view_id"], [0, 99]) == [[sid, 1]]
    assert store.view_positions(v2["view_id"], [["a", "b"], [sid, 999]]) == {"positions": [], "missing": 1}


def test_merge_keys_carry_the_member(store, write_csv):
    a = store.ingest_csv(write_csv(ROWS, "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(ROWS, "b.csv"), name="b", build_fts=False)["id"]
    mid = store.create_merge("m", [a, b])["id"]
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": [{"column": "B", "dir": "asc"}]})
    keys = store.view_keys(v["view_id"], [0, 1])
    assert sorted(k[0] for k in keys) == sorted([a, b]) and all(k[1] == 1 for k in keys)
    assert store.view_positions(v["view_id"], keys)["positions"] == [0, 1]


def test_routes(client, store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "r.csv"), name="r", build_fts=False)["id"]
    v = client.post("/api/view", json={"source_id": sid}).json()
    keys = client.post("/api/view/keys", json={"view_id": v["view_id"], "positions": [1]}).json()["keys"]
    assert keys == [[sid, 2]]
    assert client.post("/api/view/positions", json={"view_id": v["view_id"], "keys": keys}).json() == {"positions": [1], "missing": 0}
    assert client.post("/api/view/keys", json={"view_id": "view_nope", "positions": [1]}).status_code == 409
