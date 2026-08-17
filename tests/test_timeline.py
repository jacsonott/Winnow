"""store.py's unified Timeline: build_timeline/fetch_timeline_rows —
cross-table union of tagged rows only, per-source timestamp/body/type-label
configs with sensible defaults when a source has no config, tag-subset
filtering, and chronological ordering across differently-shaped tables."""

from __future__ import annotations


def _two_tagged_sources(store, write_csv):
    p1 = write_csv(
        [
            ["Timestamp", "EventId", "User"],
            ["2026-01-05T10:00:00", "4624", "alice"],
            ["2026-01-06T11:00:00", "4625", "bob"],
        ],
        name="evtx.csv",
    )
    rec1 = store.ingest_csv(p1, name="evtx.csv", build_fts=False)

    p2 = write_csv(
        [
            ["VisitTime", "Url"],
            ["2026-01-05T09:30:00", "http://example.com"],
            ["2026-01-07T12:00:00", "http://evil.example"],
        ],
        name="history.csv",
    )
    rec2 = store.ingest_csv(p2, name="history.csv", build_fts=False)

    tags = store.list_tags()
    tag_a, tag_b = tags[0]["id"], tags[1]["id"]
    store.set_tags(rec1["id"], [1], tag_a, True)  # evtx row 1
    store.set_tags(rec1["id"], [2], tag_b, True)  # evtx row 2
    store.set_tags(rec2["id"], [2], tag_a, True)  # history row 2 (untagged row 1 excluded)

    configs = {
        rec1["id"]: {"timestamp_columns": [{"column": "Timestamp", "label": None}],
                     "body_columns": ["EventId", "User"], "type_label": "Windows Event Log"},
        rec2["id"]: {"timestamp_columns": [{"column": "VisitTime", "label": None}],
                     "body_columns": ["Url"], "type_label": "Browser History"},
    }
    return rec1["id"], rec2["id"], tag_a, tag_b, configs


def test_timeline_unions_tagged_rows_across_tables_in_chronological_order(store, write_csv):
    rec1_id, rec2_id, tag_a, tag_b, configs = _two_tagged_sources(store, write_csv)

    res = store.build_timeline(configs=configs)
    assert res["row_count"] == 3
    rows = store.fetch_timeline_rows(res["view_id"], 0, 20)["rows"]

    # Untagged rows never appear at all.
    bodies = [r["body"] for r in rows]
    assert "http://example.com" not in bodies

    # Chronological across BOTH tables, using each one's own timestamp column.
    assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)
    assert rows[0]["source_name"] == "evtx.csv" and rows[0]["body"] == "4624 | alice"
    assert rows[1]["source_name"] == "evtx.csv" and rows[1]["body"] == "4625 | bob"
    assert rows[2]["source_name"] == "history.csv" and rows[2]["body"] == "http://evil.example"
    assert rows[0]["type_label"] == "Windows Event Log"
    assert rows[2]["type_label"] == "Browser History"


def test_timeline_tag_filter_narrows_to_matching_rows_only(store, write_csv):
    rec1_id, rec2_id, tag_a, tag_b, configs = _two_tagged_sources(store, write_csv)

    res = store.build_timeline(configs=configs, tag_ids=[tag_a])
    rows = store.fetch_timeline_rows(res["view_id"], 0, 20)["rows"]
    assert len(rows) == 2
    assert all(tag_a in r["tags"] for r in rows)
    assert not any(tag_b in r["tags"] for r in rows)


def test_timeline_falls_back_to_defaults_without_a_config(store, write_csv):
    rec1_id, rec2_id, tag_a, tag_b, configs = _two_tagged_sources(store, write_csv)

    res = store.build_timeline()  # no configs at all
    rows = store.fetch_timeline_rows(res["view_id"], 0, 20)["rows"]
    assert len(rows) == 3
    # Falls back to the source's own file name as the type label...
    assert {r["type_label"] for r in rows} == {"evtx.csv", "history.csv"}
    # ...and every column (since no body_columns override), including the
    # timestamp column itself, joined into the body.
    evtx_row = next(r for r in rows if r["source_name"] == "evtx.csv" and "4624" in r["body"])
    assert evtx_row["body"] == "2026-01-05T10:00:00 | 4624 | alice"


def test_timeline_excludes_sources_with_no_tagged_rows(store, write_csv):
    p = write_csv([["A", "B"], ["1", "2"]], name="untagged.csv")
    store.ingest_csv(p, name="untagged.csv", build_fts=False)
    res = store.build_timeline()
    assert res["row_count"] == 0
    assert store.fetch_timeline_rows(res["view_id"], 0, 20)["rows"] == []


def test_timeline_rebuild_evicts_the_previous_timeline_view(store, write_csv):
    _two_tagged_sources(store, write_csv)
    first = store.build_timeline()
    second = store.build_timeline()
    assert first["view_id"] != second["view_id"]
    assert first["view_id"] not in store._views


def test_fetch_timeline_rows_raises_after_view_expires(store, write_csv):
    import pytest

    _two_tagged_sources(store, write_csv)
    res = store.build_timeline()
    store.close_view(res["view_id"])
    with pytest.raises(KeyError):
        store.fetch_timeline_rows(res["view_id"], 0, 10)


def test_timeline_api_routes(client, write_csv):
    import server

    rec1_id, rec2_id, tag_a, tag_b, configs = _two_tagged_sources(server.STORE, write_csv)

    baseline = len(client.get("/api/timeline_templates").json())  # shipped KAPE defaults

    for source_id, cfg in configs.items():
        src = server.STORE.get_source(source_id)
        col_names = [c["name"] for c in src["columns"]]
        r = client.post("/api/timeline_templates", json={
            "col_names": col_names, "type_label": cfg["type_label"],
            "timestamp_columns": cfg["timestamp_columns"], "body_columns": cfg["body_columns"],
        })
        assert r.status_code == 200

    listed = client.get("/api/timeline_templates").json()
    assert len(listed) == baseline + 2

    r = client.post("/api/timeline", json={"tag_ids": []})
    assert r.status_code == 200
    view = r.json()
    assert view["row_count"] == 3

    r2 = client.get(f"/api/timeline_rows?view_id={view['view_id']}&start=0&count=10")
    assert r2.status_code == 200
    rows = r2.json()["rows"]
    assert len(rows) == 3
    assert rows[0]["body"] == "4624 | alice"  # template's body_columns applied via the API path too

    del_id = next(t["id"] for t in listed if t["type_label"] == "Windows Event Log")
    r3 = client.delete(f"/api/timeline_templates/{del_id}")
    assert r3.status_code == 200
    assert len(client.get("/api/timeline_templates").json()) == baseline + 1
