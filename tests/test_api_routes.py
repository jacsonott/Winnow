"""server.py HTTP layer: the CSRF middleware, and a risk-weighted slice of
routes that do real request-shape work beyond a thin Store pass-through
(most routes are exactly that, and are already covered by the Store-level
tests in the other files — this file's job is the HTTP wiring itself)."""

from __future__ import annotations

import io
import json


def test_csrf_middleware_blocks_post_without_header(store, monkeypatch):
    import server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "STORE", store)
    bare_client = TestClient(server.app)  # no X-Timeline-Lite-Client header by default
    r = bare_client.post("/api/tags", json={"name": "X", "color": "#000000"})
    assert r.status_code == 403


def test_static_assets_and_index_are_not_browser_cached(client):
    # No build step / content-hashed filenames to cache-bust with (see
    # CLAUDE.md) and these files change often during active development —
    # a stale style.css/app.js served from the browser's own disk cache
    # (StaticFiles sets ETag/Last-Modified but no Cache-Control on its own)
    # was a real, reproduced bug: a hard refresh "fixed" a layout issue
    # that the actual CSS on disk never had.
    for path in ("/", "/static/style.css", "/static/app.js", "/static/index.html"):
        r = client.get(path)
        assert r.headers.get("cache-control") == "no-cache", path
    # API routes are untouched — no reason to force revalidation on data.
    r = client.get("/api/sources")
    assert r.headers.get("cache-control") is None


def test_csrf_middleware_exempts_get(store, monkeypatch):
    import server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "STORE", store)
    bare_client = TestClient(server.app)
    r = bare_client.get("/api/sources")
    assert r.status_code == 200


def test_csrf_middleware_allows_post_with_header(client):
    r = client.post("/api/tags", json={"name": "X", "color": "#000000"})
    assert r.status_code == 200


def test_ingest_upload_route(client):
    csv_bytes = b"A,B\n1,2\n3,4\n"
    r = client.post(
        "/api/ingest/upload",
        files={"file": ("up.csv", io.BytesIO(csv_bytes), "text/csv")},
        data={"build_fts": "false"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["row_count"] == 2
    assert [c["name"] for c in body["columns"]] == ["A", "B"]


def test_saved_filters_reorder_route(client):
    a = client.post("/api/saved_filters", json={"name": "A", "col_names": ["X"], "payload": {}}).json()
    b = client.post("/api/saved_filters", json={"name": "B", "col_names": ["X"], "payload": {}}).json()
    r = client.post("/api/saved_filters/reorder", json={"ids": [b["id"], a["id"]]})
    assert r.status_code == 200
    assert [f["name"] for f in r.json()] == ["B", "A"]


def test_saved_filter_update_route_takes_a_payload_edit(client):
    """The Filter builder's "Update" button re-saves conditions over an
    existing filter and deliberately omits col_names, so the filter stays
    bound to the header set it was saved for."""
    created = client.post("/api/saved_filters", json={
        "name": "Failed logons", "col_names": ["EventId"], "payload": {"search": "4625"},
    }).json()

    r = client.put(f"/api/saved_filters/{created['id']}", json={"payload": {"search": "4624"}})
    assert r.status_code == 200
    updated = r.json()
    assert updated["payload"] == {"search": "4624"}
    assert updated["name"] == "Failed logons"
    assert updated["col_names"] == ["EventId"]


def test_saved_filter_update_route_404s_unknown_id(client):
    r = client.put("/api/saved_filters/9999", json={"name": "nope"})
    assert r.status_code == 404


def test_ingest_sqlite_routes_preview_then_upload(client, tmp_path):
    import sqlite3

    path = tmp_path / "sample.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO widgets VALUES (?,?)", [(1, "a"), (2, "b")])
    conn.commit()
    conn.close()
    raw = path.read_bytes()

    r = client.post(
        "/api/ingest/sqlite/preview",
        files={"file": ("sample.sqlite", io.BytesIO(raw), "application/octet-stream")},
    )
    assert r.status_code == 200
    tables = {t["name"]: t for t in r.json()["tables"]}
    assert tables["widgets"]["row_count"] == 2

    r2 = client.post(
        "/api/ingest/sqlite/upload",
        files={"file": ("sample.sqlite", io.BytesIO(raw), "application/octet-stream")},
        data={"table": "widgets", "build_fts": "false"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["row_count"] == 2
    assert [c["name"] for c in body["columns"]] == ["id", "name"]


def test_ingest_json_routes_preview_then_upload(client):
    records = [
        {"id": 1, "user": {"name": "alice"}},
        {"id": 2, "user": {"name": "bob"}},
    ]
    raw = json.dumps(records).encode()

    r = client.post(
        "/api/ingest/json/preview",
        files={"file": ("data.json", io.BytesIO(raw), "application/json")},
        data={"flatten_mode": "full"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["record_count"] == 2
    assert body["columns"] == ["id", "user.name"]

    r2 = client.post(
        "/api/ingest/json/upload",
        files={"file": ("data.json", io.BytesIO(raw), "application/json")},
        data={"flatten_mode": "full", "build_fts": "false"},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["row_count"] == 2
    assert [c["name"] for c in body2["columns"]] == ["id", "user.name"]


def test_ingest_jsonl_route_via_extension(client):
    records = [{"id": 1}, {"id": 2}, {"id": 3}]
    raw = "\n".join(json.dumps(r) for r in records).encode()
    r = client.post(
        "/api/ingest/json/upload",
        files={"file": ("data.jsonl", io.BytesIO(raw), "application/x-ndjson")},
        data={"build_fts": "false"},
    )
    assert r.status_code == 200
    assert r.json()["row_count"] == 3


def test_browse_dir_lists_subdirectories(client, tmp_path):
    (tmp_path / "sub_b").mkdir()
    (tmp_path / "sub_a").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "not_a_dir.txt").write_text("x")

    r = client.get(f"/api/browse_dir?path={tmp_path}")
    assert r.status_code == 200
    body = r.json()
    assert body["dirs"] == ["sub_a", "sub_b"]  # sorted, dotfiles/files excluded
    assert body["parent"] == str(tmp_path.parent)


def test_browse_dir_rejects_non_directory(client, tmp_path):
    f = tmp_path / "not_a_dir.txt"
    f.write_text("x")
    r = client.get(f"/api/browse_dir?path={f}")
    assert r.status_code == 400


def test_case_open_migrates_legacy_presets_into_saved_filters(client, store, case_path):
    # Simulate a case file saved before presets were folded into saved
    # filters — a row straight in filter_presets, the old case-scoped table.
    with store.lock, store.db:
        store.db.execute(
            "INSERT INTO filter_presets(name, col_sig, col_names, payload, created_at) VALUES (?,?,?,?,?)",
            ("Legacy preset", "sig", json.dumps(["A", "B"]), json.dumps({"search": "x"}), "2024-01-01T00:00:00"),
        )
    store.close()  # release the file so /api/case/open can open its own connection

    r = client.post("/api/case/open", json={"path": case_path})
    assert r.status_code == 200

    import workspace as WS
    migrated = next((f for f in WS.filters.list() if f["name"] == "Legacy preset"), None)
    assert migrated is not None
    assert migrated["col_names"] == ["A", "B"]
    assert migrated["payload"] == {"search": "x"}

    # One-time: the legacy table is now empty, so re-opening again doesn't
    # duplicate the migrated filter.
    r2 = client.post("/api/case/open", json={"path": case_path})
    assert r2.status_code == 200
    assert len([f for f in WS.filters.list() if f["name"] == "Legacy preset"]) == 1


def test_rows_route_returns_409_after_view_expires(client, ingested):
    store, source_id = ingested
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    store.close_view(view["view_id"])
    r = client.get(f"/api/rows?view_id={view['view_id']}&start=0&count=10")
    assert r.status_code == 409


def test_group_summary_route_accepts_json_encoded_path(client, store, write_csv):
    p = write_csv([["Host", "User"], ["H1", "a"], ["H1", "b"], ["H2", "a"]])
    rec = store.ingest_csv(p, name="g.csv", build_fts=False)
    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": []})

    r = client.get(f"/api/group_summary?view_id={view['view_id']}&column=Host")
    assert r.status_code == 200
    assert {g["value"]: g["count"] for g in r.json()["groups"]} == {"H1": 2, "H2": 1}

    path_param = json.dumps([{"column": "Host", "value": "H1"}])
    r2 = client.get(f"/api/group_summary?view_id={view['view_id']}&column=User&path={path_param}")
    assert r2.status_code == 200
    assert {g["value"]: g["count"] for g in r2.json()["groups"]} == {"a": 1, "b": 1}


def test_column_layouts_find_and_save_routes(client):
    empty = client.get("/api/column_layouts/find?col_names=A&col_names=B")
    assert empty.status_code == 200
    assert empty.json() == {}

    saved = client.post("/api/column_layouts", json={
        "col_names": ["A", "B"], "order": ["B", "A"], "columns": {},
    })
    assert saved.status_code == 200

    found = client.get("/api/column_layouts/find?col_names=B&col_names=A")
    assert found.json()["order"] == ["B", "A"]


def test_search_all_route_with_terms(client, store, write_csv):
    p = write_csv([["Process"], ["svchost.exe"], ["cmd.exe"]])
    store.ingest_csv(p, name="s.csv")
    r = client.post("/api/search_all", json={"terms": [{"term": "svchost.exe", "connector": "AND", "exclude": False}]})
    assert r.status_code == 200
    hits = r.json()
    assert len(hits) == 1
    assert hits[0]["match_count"] == 1


def test_search_all_job_routes(client, store, write_csv):
    """The start route returns immediately with a job id; the poll route
    reports progress and, once done, the same hits the sync route gives."""
    p = write_csv([["Process"], ["svchost.exe"], ["cmd.exe"]])
    store.ingest_csv(p, name="s.csv")

    started = client.post("/api/search_all/start", json={"terms": [
        {"term": "svchost.exe", "connector": "AND", "exclude": False},
    ]})
    assert started.status_code == 200
    job_id = started.json()["job_id"]

    store.wait_for_search_all_job(timeout=10)
    polled = client.get(f"/api/search_all/job?job_id={job_id}")
    assert polled.status_code == 200
    body = polled.json()
    assert body["done"] is True
    assert len(body["hits"]) == 1
    assert body["hits"][0]["match_count"] == 1
    assert body["scanned"] == body["total"] == 1


def test_search_all_job_route_404s_a_superseded_job(client, store, write_csv):
    """The poller has to be able to tell "this job is gone" from "no matches"
    — a stale poller must stop rather than render an empty result set."""
    store.ingest_csv(write_csv([["Process"], ["svchost.exe"]]), name="s.csv")
    first = client.post("/api/search_all/start", json={"query": "svchost"}).json()
    client.post("/api/search_all/start", json={"query": "cmd"})
    store.wait_for_search_all_job(timeout=10)

    assert client.get(f"/api/search_all/job?job_id={first['job_id']}").status_code == 404


def test_search_all_cancel_route(client, store, write_csv):
    store.ingest_csv(write_csv([["Process"], ["svchost.exe"]]), name="s.csv")
    started = client.post("/api/search_all/start", json={"query": "svchost"}).json()
    r = client.post(f"/api/search_all/cancel?job_id={started['job_id']}")
    assert r.status_code == 200
    assert r.json()["cancelled"] is True
    store.wait_for_search_all_job(timeout=10)


def test_sql_route_blocks_forbidden_statement(client, ingested):
    r = client.post("/api/sql", json={"sql": "PRAGMA table_info(sources)"})
    assert r.status_code == 400


def test_sql_route_runs_select(client, ingested):
    store, source_id = ingested
    r = client.post("/api/sql", json={"sql": f"SELECT count(*) AS n FROM src_{source_id}"})
    assert r.status_code == 200
    assert r.json()["rows"] == [[4]]


def test_view_route_400s_an_analyst_fixable_filter_error(client, ingested):
    _, source_id = ingested
    r = client.post("/api/view", json={
        "source_id": source_id,
        "filter_tree": {"type": "group", "op": "AND", "children": [
            {"type": "raw", "sql": "SELECT 1"},
        ]},
    })
    assert r.status_code == 400
    assert "SELECT" in r.json()["detail"]


def test_view_route_500s_an_internal_defect(client, ingested, monkeypatch):
    """A bug in here used to surface as `400 Filter error: ...`, blaming the
    analyst's filter for something they can't fix and hiding a real defect.
    Only ValueError/KeyError (bad fragment, unknown column, missing source)
    are 400s now."""
    import server
    from fastapi.testclient import TestClient

    store, source_id = ingested

    def boom(*a, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(store, "build_view", boom)
    # raise_server_exceptions=False so the unhandled error comes back as a
    # 500 response rather than being re-raised into the test.
    lenient = TestClient(server.app, headers={"X-Timeline-Lite-Client": "1"},
                         raise_server_exceptions=False)
    r = lenient.post("/api/view", json={"source_id": source_id})
    assert r.status_code == 500


def test_view_route_404_style_missing_source_is_still_a_400(client):
    r = client.post("/api/view", json={"source_id": 9999})
    assert r.status_code == 400


def test_column_index_routes_list_and_drop(client, ingested):
    store, source_id = ingested
    assert client.get(f"/api/column_indexes?source_id={source_id}").json() == []
    client.get(f"/api/column_values?source_id={source_id}&column=Process")
    assert store.wait_for_column_index(source_id, "Process", timeout=5)
    listed = client.get(f"/api/column_indexes?source_id={source_id}").json()
    assert [ix["column"] for ix in listed] == ["Process"]

    r = client.delete(f"/api/column_indexes?source_id={source_id}&column=Process")
    assert r.status_code == 200
    assert client.get(f"/api/column_indexes?source_id={source_id}").json() == []

    assert client.delete(f"/api/column_indexes?source_id={source_id}&column=Nope").status_code == 404


def test_compact_route_reports_sizes(client, ingested):
    r = client.post("/api/case/compact")
    assert r.status_code == 200
    body = r.json()
    assert body["before_bytes"] > 0
    assert body["reclaimed_bytes"] == max(0, body["before_bytes"] - body["after_bytes"])


def test_compact_route_400s_when_disk_is_full(client, ingested, monkeypatch):
    import shutil as shutil_module

    import store as store_module

    fake = shutil_module.disk_usage(".")._replace(free=1024)
    monkeypatch.setattr(store_module.shutil, "disk_usage", lambda _p: fake)
    r = client.post("/api/case/compact")
    assert r.status_code == 400
    assert "free disk space" in r.json()["detail"]


def test_row_tags_view_route_passes_exclusions_through(client, ingested):
    store, source_id = ingested
    tag = client.post("/api/tags", json={"name": "Bulk", "color": "#123456"}).json()
    view = client.post("/api/view", json={"source_id": source_id}).json()
    r = client.post("/api/row_tags/view", json={
        "view_id": view["view_id"], "tag_id": tag["id"], "on": True,
        "exclude": [[source_id, 1]],
    })
    assert r.status_code == 200
    assert r.json()["affected"] == 3
    tagged = {row[0] for row in store.db.execute(
        "SELECT rid FROM row_tags WHERE source_id=? AND tag_id=?", (source_id, tag["id"]))}
    assert tagged == {2, 3, 4}


def test_request_against_closed_store_is_409_not_500(client, ingested):
    """A request that reaches a Store whose connection was closed (a case
    switch landed mid-flight — seen for real with several browsers on one
    --host 0.0.0.0 server) must come back as a clean 409 'case closed'
    rather than an unhandled sqlite3.ProgrammingError traceback. The
    message deliberately avoids the word 'expired': app.js auto-rebuilds
    on 409s that contain it, and a stale tab auto-rebuilding against a
    newly opened case could show the wrong case's data."""
    store, source_id = ingested
    store.close()
    r = client.post("/api/view", json={"source_id": source_id})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "closed" in detail
    assert "expired" not in detail.lower()
    # GET endpoints go through the same handler
    r = client.get("/api/sources")
    assert r.status_code == 409


def test_case_open_failure_keeps_current_case_usable(client, ingested, tmp_path):
    """Opening a bad case file must not tear down the one that's open:
    the old close-then-open order left STORE pointing at a closed
    connection whenever the open failed."""
    bad = tmp_path / "not_a_case.db"
    bad.write_text("this is not a sqlite database, not even close")
    r = client.post("/api/case/open", json={"path": str(bad)})
    assert r.status_code == 400
    # the previously-open case must still answer
    r = client.get("/api/sources")
    assert r.status_code == 200
    assert len(r.json()) == 1
