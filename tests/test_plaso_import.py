"""Plaso storage (.plaso) import: both on-disk generations are read by
inspection (serialized-event era with zlib-compressed JSON blobs, and the
acstore era with schema columns on `event`), events land ordered by
timestamp with the fixed column shape plus an Attributes (JSON) catch-all,
and the file routes as its own ingest-job kind."""

from __future__ import annotations

import json
import sqlite3
import zlib

import pytest

from winnow import plasoread
from winnow.store import IngestCancelled


def _zc(doc: dict) -> bytes:
    return zlib.compress(json.dumps(doc).encode("utf-8"))


def make_old_plaso(path):
    """Serialized-event era: event(_identifier,_timestamp,_data BLOB·zlib),
    event_data likewise, row references by _event_data_row_identifier."""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE metadata (key TEXT, value TEXT);
        CREATE TABLE event (_identifier INTEGER PRIMARY KEY AUTOINCREMENT,
                            _timestamp BIGINT, _data BLOB);
        CREATE TABLE event_data (_identifier INTEGER PRIMARY KEY AUTOINCREMENT, _data BLOB);
    """)
    db.executemany("INSERT INTO metadata VALUES (?,?)", [
        ("format_version", "20170707"), ("serialization_format", "json"),
        ("compression_format", "zlib"), ("storage_type", "session")])
    db.execute("INSERT INTO event_data(_data) VALUES (?)", (_zc({
        "__container_type__": "event_data", "__type__": "AttributeContainer",
        "data_type": "windows:evtx:record", "parser": "winevtx",
        "display_name": "OS:/Windows/System32/winevt/Logs/Security.evtx",
        "hostname": "WKSTN-014", "username": "jsmith",
        "event_identifier": 4624,
        "strings": ["a", "b"],
        "raw": {"__type__": "bytes", "stream": "QUJD"},   # b"ABC"
    }),))
    db.execute("INSERT INTO event_data(_data) VALUES (?)", (_zc({
        "__container_type__": "event_data", "__type__": "AttributeContainer",
        "data_type": "fs:stat", "parser": "filestat",
        "display_name": "OS:/tmp/x", "filename": "/tmp/x",
    }),))
    events = [
        (1_700_000_120_000_000, {"timestamp": 1_700_000_120_000_000,
                                 "timestamp_desc": "Content Modification Time",
                                 "_event_data_row_identifier": 2}),
        (1_700_000_060_000_000, {"timestamp": 1_700_000_060_000_000,
                                 "timestamp_desc": "Creation Time",
                                 "_event_data_row_identifier": 1}),
    ]
    for ts, doc in events:
        db.execute("INSERT INTO event(_timestamp, _data) VALUES (?,?)",
                   (ts, _zc({"__container_type__": "event", "__type__": "AttributeContainer", **doc})))
    db.commit()
    db.close()


def make_new_plaso(path):
    """acstore era: schema columns on event, TEXT JSON on event_data (no
    compression), string identifiers, path spec via event_data_stream."""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE metadata (key TEXT, value TEXT);
        CREATE TABLE event (_identifier INTEGER PRIMARY KEY AUTOINCREMENT,
                            _event_data_identifier TEXT, date_time TEXT,
                            timestamp BIGINT, timestamp_desc TEXT);
        CREATE TABLE event_data (_identifier INTEGER PRIMARY KEY AUTOINCREMENT, _data TEXT);
        CREATE TABLE event_data_stream (_identifier INTEGER PRIMARY KEY AUTOINCREMENT, _data TEXT);
    """)
    db.executemany("INSERT INTO metadata VALUES (?,?)", [
        ("format_version", "20230327"), ("serialization_format", "json"),
        ("storage_type", "session")])
    db.execute("INSERT INTO event_data_stream(_data) VALUES (?)", (json.dumps({
        "path_spec": {"__type__": "PathSpec", "type_indicator": "NTFS",
                      "location": "/Windows/Prefetch/RUNDLL32.EXE-ABC.pf",
                      "parent": {"__type__": "PathSpec", "type_indicator": "OS",
                                 "location": "/evidence/c.E01"}},
    }),))
    db.execute("INSERT INTO event_data(_data) VALUES (?)", (json.dumps({
        "data_type": "windows:prefetch:execution", "parser": "prefetch",
        "executable_filename": "RUNDLL32.EXE", "run_count": 7,
        "_event_data_stream_identifier": "event_data_stream.1",
    }),))
    db.execute(
        "INSERT INTO event(_event_data_identifier, date_time, timestamp, timestamp_desc)"
        " VALUES (?,?,?,?)",
        ("event_data.1", json.dumps({"__type__": "DateTimeValues"}),
         1_700_000_200_000_000, "Last Time Executed"))
    db.commit()
    db.close()


def test_is_plaso_storage_detects_and_rejects(tmp_path, write_csv):
    plaso = tmp_path / "t.plaso"
    make_old_plaso(plaso)
    assert plasoread.is_plaso_storage(str(plaso))
    assert not plasoread.is_plaso_storage(write_csv([["a"], ["1"]], "x.csv"))
    other = tmp_path / "other.db"
    sqlite3.connect(other).executescript("CREATE TABLE t (a);")
    assert not plasoread.is_plaso_storage(str(other))


def test_old_format_ingests_chronologically_with_lifted_columns(store, tmp_path):
    plaso = tmp_path / "old.plaso"
    make_old_plaso(plaso)
    rec = store.ingest_plaso(str(plaso), build_fts=False)
    assert [c["name"] for c in rec["columns"]] == plasoread.PLASO_COLUMNS
    assert rec["columns"][0]["type"] == "datetime"
    assert rec["row_count"] == 2
    rows = store.run_sql(f"SELECT * FROM src_{rec['id']} ORDER BY rid")["rows"]
    # Ordered by timestamp, not by event-table row order.
    assert rows[0][1] == "2023-11-14 22:14:20.000000"      # Datetime
    assert rows[0][2] == "Creation Time"
    assert rows[0][3] == "windows:evtx:record"
    assert rows[0][4] == "winevtx"                          # Parser
    assert rows[0][5].endswith("Security.evtx")             # Source file
    assert rows[0][6] == "WKSTN-014" and rows[0][7] == "jsmith"
    extras = json.loads(rows[0][8])
    assert extras["event_identifier"] == 4624
    assert extras["strings"] == ["a", "b"]
    assert extras["raw"] == "<3 bytes>"                     # decoded bytes tag
    assert "data_type" not in extras                        # lifted, not duplicated
    assert rows[1][2] == "Content Modification Time"


def test_new_format_resolves_identifiers_and_stream_path(store, tmp_path):
    plaso = tmp_path / "new.plaso"
    make_new_plaso(plaso)
    rec = store.ingest_plaso(str(plaso), build_fts=False)
    assert rec["row_count"] == 1
    row = store.run_sql(f"SELECT * FROM src_{rec['id']}")["rows"][0]
    assert row[1] == "2023-11-14 22:16:40.000000"
    assert row[2] == "Last Time Executed"
    assert row[3] == "windows:prefetch:execution"
    # No display_name on event_data → Source file comes from the stream's
    # path spec (the innermost location).
    assert row[5] == "/Windows/Prefetch/RUNDLL32.EXE-ABC.pf"
    extras = json.loads(row[8])
    assert extras["run_count"] == 7
    assert extras["executable_filename"] == "RUNDLL32.EXE"
    assert "_event_data_stream_identifier" not in extras


def test_not_a_plaso_file_is_a_clean_error(store, tmp_path):
    other = tmp_path / "notplaso.plaso"
    sqlite3.connect(other).executescript("CREATE TABLE t (a);")
    with pytest.raises(ValueError, match="not a Plaso storage file"):
        store.ingest_plaso(str(other))


def test_plaso_routes_as_its_own_job_kind(client, store, tmp_path):
    plaso = tmp_path / "route.plaso"
    make_old_plaso(plaso)
    r = client.post("/api/ingest/jobs/path", json={"path": str(plaso)})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["kind"] == "plaso"
    done = store.wait_for_ingest_job(job["job_id"], timeout=30)
    assert done["status"] == "done"
    src = store.get_source(done["source_ids"][0])
    assert src["row_count"] == 2
    assert src["name"] == "route.plaso"


def test_directory_scan_maps_plaso_kind(store, tmp_path):
    plaso = tmp_path / "scan" / "case.plaso"
    plaso.parent.mkdir()
    make_old_plaso(plaso)
    (tmp_path / "scan" / "notes.csv").write_text("a,b\n1,2\n")
    res = store.scan_import_directory(str(tmp_path / "scan"),
                                      extensions=[".csv", ".plaso"])
    kinds = {m["rel_path"]: m["kind"] for m in res["matched"]}
    assert kinds["case.plaso"] == "plaso"
    assert kinds["notes.csv"] == "csv"


def test_cancel_drops_the_partial_source(store, tmp_path):
    plaso = tmp_path / "big.plaso"
    # Enough events to span several batches.
    db = sqlite3.connect(plaso)
    db.executescript("""
        CREATE TABLE metadata (key TEXT, value TEXT);
        CREATE TABLE event (_identifier INTEGER PRIMARY KEY AUTOINCREMENT,
                            _timestamp BIGINT, _data BLOB);
        CREATE TABLE event_data (_identifier INTEGER PRIMARY KEY AUTOINCREMENT, _data BLOB);
    """)
    db.executemany("INSERT INTO metadata VALUES (?,?)", [
        ("format_version", "20170707"), ("serialization_format", "json"),
        ("compression_format", "zlib")])
    db.execute("INSERT INTO event_data(_data) VALUES (?)", (_zc({"data_type": "t"}),))
    db.executemany("INSERT INTO event(_timestamp,_data) VALUES (?,?)", [
        (i, _zc({"timestamp": i, "timestamp_desc": "d", "_event_data_row_identifier": 1}))
        for i in range(1, 25_001)])
    db.commit()
    db.close()
    before = {s["id"] for s in store.list_sources()}
    with pytest.raises(IngestCancelled):
        store.ingest_plaso(str(plaso), build_fts=False, cancel=lambda: True)
    assert {s["id"] for s in store.list_sources()} == before
