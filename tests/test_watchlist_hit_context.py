"""Watchlist hits carry where they matched: the column and cell that held
the indicator, and the row as one line — found by re-reading the hit
rows, so nothing is stored and old cases get it too."""


def test_hits_carry_column_value_and_preview(client, store, write_csv):
    sid = store.ingest_csv(write_csv(
        [["Host", "Cmd", "Note"], ["WKS07", "RClone.exe copy", ""], ["WKS01", "notepad", "rclone was here"], ["WKS02", "", ""]], "e.csv"),
        build_fts=False)["id"]
    wid = client.post("/api/watchlist", json={"value": "rclone", "kind": "filename"}).json()["id"]
    assert client.post(f"/api/watchlist/scan?source_id={sid}").json()["matched"][str(wid)] == 2
    hits = client.get(f"/api/watchlist/hits?watchlist_id={wid}").json()
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
    (h,) = client.get(f"/api/watchlist/hits?watchlist_id={wid}").json()
    assert h["column"] == "A" and len(h["value"]) == store.WATCHLIST_PREVIEW_CHARS
    assert len(h["preview"]) == store.WATCHLIST_PREVIEW_CHARS
    # An indicator whose value changed after the scan: the hit stays, the column is None
    store.db.execute("UPDATE watchlist SET value='other' WHERE id=?", (wid,))
    store.db.commit()
    (h,) = client.get(f"/api/watchlist/hits?watchlist_id={wid}").json()
    assert h["column"] is None and h["value"] is None and h["preview"].startswith("x")
