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
        rec1["id"]: {"timestamp_column": "Timestamp", "body_columns": ["EventId", "User"], "type_label": "Windows Event Log"},
        rec2["id"]: {"timestamp_column": "VisitTime", "body_columns": ["Url"], "type_label": "Browser History"},
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

    for source_id, cfg in configs.items():
        src = server.STORE.get_source(source_id)
        col_names = [c["name"] for c in src["columns"]]
        r = client.post("/api/timeline_templates", json={
            "col_names": col_names, "type_label": cfg["type_label"],
            "timestamp_column": cfg["timestamp_column"], "body_columns": cfg["body_columns"],
        })
        assert r.status_code == 200

    listed = client.get("/api/timeline_templates").json()
    assert len(listed) == 2

    r = client.post("/api/timeline", json={"tag_ids": []})
    assert r.status_code == 200
    view = r.json()
    assert view["row_count"] == 3

    r2 = client.get(f"/api/timeline_rows?view_id={view['view_id']}&start=0&count=10")
    assert r2.status_code == 200
    rows = r2.json()["rows"]
    assert len(rows) == 3
    assert rows[0]["body"] == "4624 | alice"  # template's body_columns applied via the API path too

    del_id = listed[0]["id"]
    r3 = client.delete(f"/api/timeline_templates/{del_id}")
    assert r3.status_code == 200
    # The other one is what is left, not the one asked for.
    assert [t["id"] for t in client.get("/api/timeline_templates").json()] == [listed[1]["id"]]


# ------------------------------------------- configs and derived columns
#
# Configure tables… saves a config under the table's OWN columns
# (timeline.js leaves derived ones out of the key), and the server used to
# look it up under every column, derived included — so the first derived
# column added to a table silently dropped it back to the defaults. These
# go through the routes, with the payload the dialog sends, because the
# disagreement was between the two sides.

def _proc_table(store, write_csv):
    """Logged is the file's own datetime column; Epoch is a different
    moment, so a timestamp read off the derived column built from it is
    tellable apart from the fallback (the first datetime column)."""
    p = write_csv([
        ["Logged", "Epoch", "Process", "User"],
        ["2026-03-10 10:00:00", "1700000000", "evil.exe", "alice"],
        ["2026-03-11 10:00:00", "1700086400", "cmd.exe", "bob"],
    ], name="proc.csv")
    sid = store.ingest_csv(p, name="proc.csv", build_fts=False)["id"]
    tag = store.list_tags()[0]["id"]
    store.set_tags(sid, [1], tag, True)
    return sid


def _add_derived(store, sid, name="When"):
    res = store.add_derived_column(sid, name, "Epoch", "unix_epoch", {})
    store.wait_for_ingest_job(res["job_id"], timeout=30)


def _save_config(client, store, sid, *, timestamp, body, label="Process log", base_only=True):
    """What the dialog posts: the table's own columns as the key."""
    cols = [c["name"] for c in store.get_source(sid)["columns"]
            if not (base_only and c.get("derived"))]
    r = client.post("/api/timeline_templates", json={
        "col_names": cols, "type_label": label,
        "timestamp_column": timestamp, "body_columns": body})
    assert r.status_code == 200


def _timeline_rows(client):
    view = client.post("/api/timeline", json={"tag_ids": []}).json()
    return client.get(f"/api/timeline_rows?view_id={view['view_id']}&start=0&count=10").json()["rows"]


def test_a_derived_column_does_not_drop_the_tables_timeline_config(client, store, write_csv):
    sid = _proc_table(store, write_csv)
    _save_config(client, store, sid, timestamp="Logged", body=["Process"])
    assert _timeline_rows(client)[0]["type_label"] == "Process log"

    _add_derived(store, sid)

    [row] = _timeline_rows(client)
    # The symptom: these came back as "proc.csv" and every column joined.
    assert row["type_label"] == "Process log"
    assert row["body"] == "evil.exe"
    assert row["ts"] == "2026-03-10 10:00:00"


def test_a_derived_column_can_be_the_configured_timestamp(client, store, write_csv):
    """The dialog lists derived datetime columns in its timestamp select,
    so a config naming one has to be found under the same key as any
    other — and then read off the derived column, not the first datetime
    column the defaults would pick."""
    sid = _proc_table(store, write_csv)
    _add_derived(store, sid)
    _save_config(client, store, sid, timestamp="When", body=["Process", "User"])

    [row] = _timeline_rows(client)
    assert row["ts"] == "2023-11-14 22:13:20"
    assert row["type_label"] == "Process log"
    assert row["body"] == "evil.exe | alice"


def test_a_config_keyed_on_every_column_still_applies(client, store, write_csv):
    """Nothing that matched before the base-column key stops matching: a
    template saved under the full column list, derived included, is still
    honoured when no base-column template exists."""
    sid = _proc_table(store, write_csv)
    _add_derived(store, sid)
    _save_config(client, store, sid, timestamp="When", body=["User"], base_only=False)

    [row] = _timeline_rows(client)
    assert row["type_label"] == "Process log"
    assert (row["ts"], row["body"]) == ("2023-11-14 22:13:20", "alice")


# A table wider than ~500 columns (flattened JSON gets there easily) used to
# fail the whole build: the body joins its columns with `||`, one chain
# SQLite parses two levels deeper per column, and past its expression depth
# limit of 1000 the INSERT raised "Expression tree is too large" — so ONE
# wide table took every other table's findings off the Timeline with it.

def _wide_table(store, write_csv, ncols=600, name="wide.csv"):
    header = ["Timestamp"] + [f"c{i}" for i in range(1, ncols)]
    row = ["2026-01-05T10:00:00"] + [f"v{i}" for i in range(1, ncols)]
    sid = store.ingest_csv(write_csv([header, row], name=name), name=name, build_fts=False)["id"]
    store.set_tags(sid, [1], store.list_tags()[0]["id"], True)
    return sid, header


def test_a_table_too_wide_for_the_body_still_builds_with_its_first_columns(store, write_csv):
    rec1_id, rec2_id, tag_a, tag_b, configs = _two_tagged_sources(store, write_csv)
    wide_id, header = _wide_table(store, write_csv)

    res = store.build_timeline(configs=configs)
    assert res["row_count"] == 4  # the three narrow findings are still there too
    [trimmed] = res["body_trimmed"]
    assert trimmed["source_id"] == wide_id and trimmed["total"] == 600
    shown = trimmed["shown"]
    assert 400 < shown < 600

    rows = store.fetch_timeline_rows(res["view_id"], 0, 10)["rows"]
    wide = next(r for r in rows if r["source_id"] == wide_id)
    assert wide["body"].split(" | ") == ["2026-01-05T10:00:00"] + [f"v{i}" for i in range(1, shown)]


def test_configured_body_columns_past_the_cap_keep_their_order(store, write_csv):
    wide_id, header = _wide_table(store, write_csv)
    body = list(reversed(header))  # every column, last first
    res = store.build_timeline(configs={wide_id: {"body_columns": body}})
    shown = res["body_trimmed"][0]["shown"]
    [row] = store.fetch_timeline_rows(res["view_id"], 0, 10)["rows"]
    assert row["body"].split(" | ")[:2] == ["v599", "v598"]
    assert len(row["body"].split(" | ")) == shown


def test_a_table_under_the_cap_is_not_trimmed(store, write_csv):
    _two_tagged_sources(store, write_csv)
    assert store.build_timeline()["body_trimmed"] == []


def test_the_cap_follows_the_linked_sqlites_depth_limit(store, write_csv):
    """Read off the connection rather than assumed, so a SQLite built with
    a lower limit still fits — and the cap is exact enough that the column
    after it would not have."""
    import sqlite3

    import pytest

    if not hasattr(store.db, "setlimit"):
        pytest.skip("Connection.setlimit needs Python 3.11+")
    wide_id, header = _wide_table(store, write_csv, ncols=150)
    store.db.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 200)
    res = store.build_timeline()
    [trimmed] = res["body_trimmed"]
    assert trimmed["shown"] == 95  # (200 - headroom 10) // 2
    [row] = store.fetch_timeline_rows(res["view_id"], 0, 10)["rows"]
    assert len(row["body"].split(" | ")) == 95


def test_watchlist_latest_hits_survive_a_table_too_wide_for_the_body(store, write_csv):
    """Latest hits joins the same body from the same config, and failed the
    same way."""
    wide_id, header = _wide_table(store, write_csv)
    store.add_indicator("v7")
    store.scan_source(wide_id)
    [hit] = store.latest_hits()["rows"]
    assert hit["source_id"] == wide_id and hit["body"].startswith("v1 | v2 | ")
