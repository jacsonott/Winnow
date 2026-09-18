"""An indicator's hits, grouped by the table they landed in.

`indicator_hits` answers `{sources, hits}`: one entry per table with its
EXACT count (a GROUP BY over watchlist_hits) and, per table, the first
`per_source_limit` hits in rid order. It used to be one flat list capped
at 500 with no ORDER BY, so a hot indicator's later tables vanished
entirely and the count on screen was whatever fit under the cap.
"""

from __future__ import annotations


def _rows(n, value, filler="nothing"):
    return [["A", "B"]] + [[value, f"r{i}"] for i in range(n)] + [[filler, "x"]] * 2


def test_counts_are_exact_while_rows_are_capped_per_table(store, write_csv):
    a = store.ingest_csv(write_csv(_rows(300, "needle"), name="a.csv"), name="a.csv", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(_rows(5, "needle"), name="b.csv"), name="b.csv", build_fts=False)["id"]
    wid = store.add_indicator("needle", "other")["id"]
    assert store.scan_all()["matched"] == {wid: 305}

    res = store.indicator_hits(wid)
    assert res["sources"] == [
        {"source_id": a, "source_name": "a.csv", "count": 300, "shown": store.WATCHLIST_HITS_PER_SOURCE},
        {"source_id": b, "source_name": "b.csv", "count": 5, "shown": 5},
    ]
    hits_a = [h for h in res["hits"] if h["source_id"] == a]
    hits_b = [h for h in res["hits"] if h["source_id"] == b]
    # The cap applies to the rows of each table, in rid order — never to
    # the count, and never to which tables are listed.
    assert [h["rid"] for h in hits_a] == list(range(1, store.WATCHLIST_HITS_PER_SOURCE + 1))
    assert [h["rid"] for h in hits_b] == [1, 2, 3, 4, 5]
    # Hits come in table order: the whole of a's slice, then b's.
    assert [h["source_id"] for h in res["hits"]] == [a] * store.WATCHLIST_HITS_PER_SOURCE + [b] * 5
    # The per-hit context still rides on every row.
    assert hits_a[0]["column"] == "A" and hits_a[0]["value"] == "needle"
    assert hits_a[0]["preview"] == "needle | r0"

    small = store.indicator_hits(wid, per_source_limit=3)
    assert [(s["source_id"], s["count"], s["shown"]) for s in small["sources"]] == [(a, 300, 3), (b, 5, 3)]
    assert [h["rid"] for h in small["hits"]] == [1, 2, 3, 1, 2, 3]
    # A limit under one still shows one row per table.
    assert len(store.indicator_hits(wid, per_source_limit=0)["hits"]) == 2


def test_tables_order_by_count_then_id(store, write_csv):
    ids = [store.ingest_csv(write_csv(_rows(n, "ioc"), name=f"t{i}.csv"), name=f"t{i}.csv", build_fts=False)["id"]
           for i, n in enumerate((2, 5, 2))]
    wid = store.add_indicator("ioc", "other")["id"]
    store.scan_all()
    order = [(s["source_id"], s["count"]) for s in store.indicator_hits(wid)["sources"]]
    assert order == [(ids[1], 5), (ids[0], 2), (ids[2], 2)]


def test_route_answers_the_grouped_shape(client, store, write_csv):
    sid = store.ingest_csv(write_csv(_rows(3, "evil.exe"), name="e.csv"), name="e.csv", build_fts=False)["id"]
    wid = client.post("/api/watchlist", json={"value": "evil.exe", "kind": "filename"}).json()["id"]
    r = client.get(f"/api/watchlist/hits?watchlist_id={wid}")
    assert r.status_code == 200
    assert r.json() == {"sources": [], "hits": []}          # not scanned yet
    client.post(f"/api/watchlist/scan?source_id={sid}")
    body = client.get(f"/api/watchlist/hits?watchlist_id={wid}").json()
    assert body["sources"] == [{"source_id": sid, "source_name": "e.csv", "count": 3, "shown": 3}]
    assert [h["rid"] for h in body["hits"]] == [1, 2, 3]
    assert client.get("/api/watchlist/hits?watchlist_id=999").json() == {"sources": [], "hits": []}


def test_a_dropped_table_takes_its_hits_with_it(store, write_csv):
    """drop_source reuses the table's id on the next import, so hits left
    keyed by it would show up under the next file's name as a phantom
    group with a count nothing in that file explains."""
    a = store.ingest_csv(write_csv(_rows(4, "needle"), name="a.csv"), name="a.csv", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(_rows(1, "needle"), name="b.csv"), name="b.csv", build_fts=False)["id"]
    wid = store.add_indicator("needle", "other")["id"]
    store.scan_all()
    assert [s["source_id"] for s in store.indicator_hits(wid)["sources"]] == [a, b]
    # The newest table is the one whose id the next import takes.
    store.drop_source(b)
    assert store.indicator_hits(wid)["sources"] == [
        {"source_id": a, "source_name": "a.csv", "count": 4, "shown": 4}]
    assert store.list_indicators()[0]["hit_count"] == 4
    again = store.ingest_csv(write_csv([["A"], ["clean"]], name="c.csv"), name="c.csv", build_fts=False)["id"]
    assert again == b   # the id came back around
    assert [s["source_id"] for s in store.indicator_hits(wid)["sources"]] == [a]   # no phantom "c.csv" group
