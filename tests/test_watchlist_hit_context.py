"""Watchlist hits carry where they matched: the column and cell that held
the indicator, and the row as one line — found by re-reading the hit
rows, so nothing is stored and old cases get it too. The route answers
`{sources, hits}` (grouped per table — tests/test_watchlist_grouped_hits.py);
the context rides on each hit."""


def test_hits_carry_column_value_and_preview(client, store, write_csv):
    sid = store.ingest_csv(write_csv(
        [["Host", "Cmd", "Note"], ["WKS07", "RClone.exe copy", ""], ["WKS01", "notepad", "rclone was here"], ["WKS02", "", ""]], "e.csv"),
        build_fts=False)["id"]
    wid = client.post("/api/watchlist", json={"value": "rclone", "kind": "filename"}).json()["id"]
    assert client.post(f"/api/watchlist/scan?source_id={sid}").json()["matched"][str(wid)] == 2
    hits = client.get(f"/api/watchlist/hits?watchlist_id={wid}").json()["hits"]
    by_rid = {h["rid"]: h for h in hits}
    assert set(by_rid) == {1, 2}
    # First column in table order that contains the value, case-insensitively; the cell verbatim
    assert by_rid[1]["column"] == "Cmd" and by_rid[1]["value"] == "RClone.exe copy"
    assert by_rid[2]["column"] == "Note" and by_rid[2]["value"] == "rclone was here"
    # The row as one line: non-empty cells joined, in column order
    assert by_rid[1]["preview"] == "WKS07 | RClone.exe copy"
    assert by_rid[2]["preview"] == "WKS01 | notepad | rclone was here"
    assert by_rid[1]["source_id"] == sid and by_rid[1]["source_name"] == "e.csv"


def test_long_cells_are_capped_and_a_stale_hit_has_no_column(client, store, write_csv):
    long = "x" * 1000 + "needle" + "y" * 1000
    sid = store.ingest_csv(write_csv([["A"], [long]], "l.csv"), build_fts=False)["id"]
    wid = client.post("/api/watchlist", json={"value": "needle", "kind": "other"}).json()["id"]
    client.post(f"/api/watchlist/scan?source_id={sid}")
    (h,) = client.get(f"/api/watchlist/hits?watchlist_id={wid}").json()["hits"]
    assert h["column"] == "A" and len(h["value"]) == store.WATCHLIST_PREVIEW_CHARS
    assert len(h["preview"]) == store.WATCHLIST_PREVIEW_CHARS
    # An indicator whose value changed after the scan: the hit stays, the column is None
    store.db.execute("UPDATE watchlist SET value='other' WHERE id=?", (wid,))
    store.db.commit()
    (h,) = client.get(f"/api/watchlist/hits?watchlist_id={wid}").json()["hits"]
    assert h["column"] is None and h["value"] is None and h["preview"].startswith("x")


def test_a_value_straddling_two_cells_is_a_hit_with_no_column(client, store, write_csv):
    """The scan matches the row's cells joined with a space, so an
    indicator can match across a cell boundary: no single column holds it,
    but the row IS a hit. The hit list says so — column None, value the
    stretch of the joined row around the match — rather than reporting the
    row as if the indicator had been edited away."""
    sid = store.ingest_csv(write_csv([["A", "B"], ["alpha", "beta"], ["nothing", "here"]], "s.csv"), build_fts=False)["id"]
    wid = client.post("/api/watchlist", json={"value": "alpha beta", "kind": "other"}).json()["id"]
    assert client.post(f"/api/watchlist/scan?source_id={sid}").json()["matched"][str(wid)] == 1
    (h,) = client.get(f"/api/watchlist/hits?watchlist_id={wid}").json()["hits"]
    assert h["rid"] == 1 and h["column"] is None
    assert h["value"] == "alpha beta" and h["preview"] == "alpha | beta"


def test_hits_are_read_while_the_writer_lock_is_held(client, store, write_csv):
    """The hit list is a read, on the reader pool (invariant #4) — it opens
    while an import holds the writer lock, rather than queueing behind it."""
    import threading
    sid = store.ingest_csv(write_csv([["A"], ["rclone"]], "w.csv"), build_fts=False)["id"]
    wid = client.post("/api/watchlist", json={"value": "rclone", "kind": "other"}).json()["id"]
    client.post(f"/api/watchlist/scan?source_id={sid}")
    out = []
    with store.lock:
        t = threading.Thread(target=lambda: out.append(store.indicator_hits(wid)))
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), "indicator_hits waited on the writer lock"
    assert out and out[0]["hits"][0]["column"] == "A"
