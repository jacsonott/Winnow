"""Choosing a table folder at import time: the new tables land in the
folder the analyst picked, instead of at the top of the sidebar to be
dragged in one at a time afterwards.

One value, `folder_id`, rides every import route — the job routes (by path
and by upload), the synchronous ones, the plugin ones — and the job worker
files the tables when they finish (Store.file_imported_sources). A
directory import's on-disk tree (`folder_path`) is reproduced *beneath* the
chosen folder. `--folder NAME` does the same for `--open` on the command
line, by name since a shell has no folder ids.

The guarantees that matter, each pinned here: a stale id is refused before
anything is read (never discovered after the rows are in); a folder
deleted while a long import runs leaves the tables at the top level and the
import done (never an import reported failed whose rows are in the case,
and never a mirrored subfolder hung off a parent that no longer exists,
which nothing in the sidebar would show); and the tables a multi-table
job finished before a cancel or a failure are filed too."""

from __future__ import annotations

import json
import sqlite3

from spawn import spawn_server


def _csv(tmp_path, rel, content="When,User\n2026-01-01 00:00:00,alice\n"):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return str(p)


def _sources(store):
    return {s["name"]: s for s in store.list_sources()}


def _folders(store):
    return {f["id"]: f for f in store.list_folders()}


def _run(store, kind, path, **options):
    job = store.start_ingest_job(kind, path, options={"build_fts": False, **options})
    done = store.wait_for_ingest_job(job["job_id"], timeout=30)
    return done


def _two_table_db(tmp_path):
    dbp = tmp_path / "ext.db"
    conn = sqlite3.connect(dbp)
    conn.execute("CREATE TABLE alpha (x TEXT)")
    conn.execute("CREATE TABLE beta (y TEXT)")
    conn.execute("INSERT INTO alpha VALUES ('a')")
    conn.execute("INSERT INTO beta VALUES ('b')")
    conn.commit()
    conn.close()
    return str(dbp)


# ------------------------------------------------------------------ the job


def test_a_job_files_its_table_in_the_chosen_folder_and_leaves_it_open(store, tmp_path):
    host = store.create_folder("WS01")
    done = _run(store, "csv", _csv(tmp_path, "Security.csv"), folder_id=host["id"])
    assert done["status"] == "done", done.get("error")
    src = _sources(store)["Security.csv"]
    assert src["folder_id"] == host["id"]
    # Filed, not hidden: the analyst imported it to look at it. Only a
    # directory import's subfolder files come in closed.
    assert src["is_open"] is True


def test_no_folder_is_the_top_level_as_before(store, tmp_path):
    store.create_folder("WS01")
    _run(store, "csv", _csv(tmp_path, "Security.csv"))
    assert _sources(store)["Security.csv"]["folder_id"] is None


def test_a_directory_import_reproduces_its_tree_beneath_the_chosen_folder(store, tmp_path):
    host = store.create_folder("WS01")
    _run(store, "csv", _csv(tmp_path, "kape/top.csv"), folder_id=host["id"], folder_path="")
    _run(store, "csv", _csv(tmp_path, "kape/EventLogs/Security.csv"),
         folder_id=host["id"], folder_path="EventLogs")
    _run(store, "csv", _csv(tmp_path, "kape/EventLogs/System.csv"),
         folder_id=host["id"], folder_path="EventLogs")
    srcs, folders = _sources(store), _folders(store)

    # A file at the scan root goes in the chosen folder itself, and stays
    # open as it always has.
    assert srcs["top.csv"]["folder_id"] == host["id"]
    assert srcs["top.csv"]["is_open"] is True
    # A subfolder's files share one mirrored folder, under the chosen one,
    # not at the top of the sidebar — and come in closed, as before.
    sub = folders[srcs["Security.csv"]["folder_id"]]
    assert sub["name"] == "EventLogs" and sub["parent_id"] == host["id"]
    assert srcs["System.csv"]["folder_id"] == sub["id"]
    assert srcs["Security.csv"]["is_open"] is False
    assert [f["name"] for f in folders.values()].count("EventLogs") == 1


def test_a_multi_table_job_files_every_table(store, tmp_path):
    host = store.create_folder("Browser")
    done = _run(store, "sqlite", _two_table_db(tmp_path), folder_id=host["id"],
                tables=[{"table": "alpha"}, {"table": "beta"}])
    assert done["status"] == "done", done.get("error")
    assert {store.get_source(i)["name"]: _folders(store)[host["id"]]["name"]
            for i in done["source_ids"]} == {"ext.alpha": "Browser", "ext.beta": "Browser"}
    assert all(s["folder_id"] == host["id"] for s in store.list_sources())


def test_the_tables_a_failed_multi_table_job_finished_are_filed_too(store, tmp_path):
    """The tables before the one that failed stay in the case (the job's
    contract); they go where the analyst asked, not to the top level."""
    host = store.create_folder("Browser")
    done = _run(store, "sqlite", _two_table_db(tmp_path), folder_id=host["id"],
                tables=[{"table": "alpha"}, {"table": "no_such_table"}])
    assert done["status"] == "error"
    srcs = store.list_sources()
    assert [s["name"] for s in srcs] == ["ext.alpha"]
    assert srcs[0]["folder_id"] == host["id"]


def test_a_folder_deleted_mid_import_leaves_the_table_at_the_top_and_the_import_done(store, tmp_path):
    host = store.create_folder("WS01")
    store.delete_folder(host["id"])
    done = _run(store, "csv", _csv(tmp_path, "kape/EventLogs/Security.csv"),
                folder_id=host["id"], folder_path="EventLogs")
    assert done["status"] == "done", done.get("error")
    src = _sources(store)["Security.csv"]
    # The mirrored subfolder is made at the top level, where the sidebar
    # shows it — not under a parent id nothing lists any more.
    folder = _folders(store)[src["folder_id"]]
    assert folder["name"] == "EventLogs" and folder["parent_id"] is None
    assert all(f["parent_id"] in (None, *_folders(store)) for f in store.list_folders())


def test_file_imported_sources_by_name_finds_or_creates(store, write_csv):
    """What --folder rides on: a path of names, found when the case has it
    (case-insensitive, as a directory import's are), created when not."""
    ws = store.create_folder("WS01")
    a = store.ingest_csv(write_csv([["A"], ["1"]], "a.csv"), build_fts=False)
    b = store.ingest_csv(write_csv([["A"], ["1"]], "b.csv"), build_fts=False)
    fid = store.file_imported_sources([a["id"], b["id"]], folder_path="ws01/EventLogs")
    sub = _folders(store)[fid]
    assert sub["name"] == "EventLogs" and sub["parent_id"] == ws["id"]
    assert {s["folder_id"] for s in store.list_sources()} == {fid}
    assert store.file_imported_sources([a["id"]]) is None   # nothing asked: nothing moves
    assert _sources(store)["a.csv"]["folder_id"] == fid


# ------------------------------------------------------------------ routes


def test_the_job_routes_take_folder_id(client, store, tmp_path):
    host = store.create_folder("WS01")
    r = client.post("/api/ingest/jobs/path", json={
        "path": _csv(tmp_path, "by-path.csv"), "build_fts": False, "folder_id": host["id"]})
    assert r.status_code == 200, r.text
    store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    r = client.post("/api/ingest/jobs/upload",
                    files={"file": ("uploaded.csv", b"When,User\n2026-01-01,bob\n")},
                    data={"kind": "csv", "build_fts": "false", "folder_id": str(host["id"])})
    assert r.status_code == 200, r.text
    store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    srcs = _sources(store)
    assert srcs["by-path.csv"]["folder_id"] == host["id"]
    assert srcs["uploaded.csv"]["folder_id"] == host["id"]


def test_a_stale_folder_id_is_refused_before_anything_is_read(client, store, tmp_path):
    gone = store.create_folder("gone")
    store.delete_folder(gone["id"])
    path = _csv(tmp_path, "Security.csv")
    for route, body in (("/api/ingest/jobs/path", {"path": path}),
                        ("/api/ingest/path", {"path": path}),
                        ("/api/ingest/json/path", {"path": _csv(tmp_path, "r.jsonl", '{"a": 1}\n')})):
        r = client.post(route, json={**body, "build_fts": False, "folder_id": gone["id"]})
        assert r.status_code == 400, (route, r.text)
        assert "folder no longer exists" in r.json()["detail"]
    r = client.post("/api/ingest/jobs/upload", files={"file": ("u.csv", b"A\n1\n")},
                    data={"kind": "csv", "folder_id": str(gone["id"])})
    assert r.status_code == 400
    r = client.post("/api/ingest/upload", files={"file": ("u.csv", b"A\n1\n")},
                    data={"folder_id": str(gone["id"])})
    assert r.status_code == 400
    assert store.list_ingest_jobs() == [] and store.list_sources() == []


def test_the_synchronous_routes_take_folder_id(client, store, tmp_path):
    host = store.create_folder("WS01")
    r = client.post("/api/ingest/path", json={
        "path": _csv(tmp_path, "sync.csv"), "build_fts": False, "folder_id": host["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["folder_id"] == host["id"]
    p = tmp_path / "recs.jsonl"
    p.write_text(json.dumps({"a": "1"}) + "\n")
    r = client.post("/api/ingest/json/path", json={"path": str(p), "build_fts": False, "folder_id": host["id"]})
    assert r.status_code == 200, r.text
    r = client.post("/api/ingest/upload", files={"file": ("up.csv", b"A\n1\n")},
                    data={"build_fts": "false", "folder_id": str(host["id"])})
    assert r.status_code == 200, r.text
    assert {s["name"]: s["folder_id"] for s in store.list_sources()} == {
        "sync.csv": host["id"], "recs.jsonl": host["id"], "up.csv": host["id"]}


# ------------------------------------------------------------- command line


def test_open_with_folder_files_the_tables(tmp_path):
    a = _csv(tmp_path, "Security.csv")
    b = _csv(tmp_path, "System.csv")
    server = spawn_server(tmp_path, "--case", tmp_path / "cli.db", "--open", a, b,
                          "--folder", "WS01/EventLogs", "--no-fts", ready="/api/sources")
    try:
        folders = {f["id"]: f for f in server.get("/api/folders")}
        filed = {s["name"]: s["folder_id"] for s in server.get("/api/sources")}
    finally:
        server.stop()
    leaf = folders[filed["Security.csv"]]
    assert leaf["name"] == "EventLogs"
    assert folders[leaf["parent_id"]]["name"] == "WS01"
    assert filed["System.csv"] == leaf["id"]
    assert len(folders) == 2   # one chain, shared by both files
