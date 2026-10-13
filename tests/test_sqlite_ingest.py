"""store.py's SQLite-file ingest path: table preview (including the
WebKit/Chrome-timestamp heuristic), and importing one table as a new
source with optional timestamp conversion. Chromium's History file is the
motivating case, but none of this is Chromium-specific — any table in any
external .db is fair game."""

from __future__ import annotations

import datetime
import sqlite3

import pytest

from winnow.store import WEBKIT_EPOCH_OFFSET_US, _webkit_to_iso


def _to_webkit(dt: datetime.datetime) -> int:
    epoch_1601 = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
    return int((dt - epoch_1601).total_seconds() * 1_000_000)


@pytest.fixture
def chromium_like_db(tmp_path):
    """A tiny stand-in for a Chromium History file: a urls table with a
    WebKit-epoch timestamp column plus ordinary columns, and an unrelated
    meta table that shouldn't be flagged as having any timestamp column."""
    path = str(tmp_path / "History")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, "
        "visit_count INTEGER, last_visit_time INTEGER)"
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = [
        (1, "https://example.com/a", "Example A", 3, _to_webkit(now - datetime.timedelta(days=1))),
        (2, "https://example.com/b", "Example B", 1, _to_webkit(now - datetime.timedelta(hours=2))),
        (3, "https://example.com/c", "Example C", 5, _to_webkit(now - datetime.timedelta(minutes=10))),
    ]
    conn.executemany("INSERT INTO urls VALUES (?,?,?,?,?)", rows)
    conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('version', '1')")
    conn.commit()
    conn.close()
    return path, now


def test_webkit_to_iso_roundtrips_a_real_timestamp():
    now = datetime.datetime.now(datetime.timezone.utc)
    iso = _webkit_to_iso(_to_webkit(now))
    assert iso is not None
    assert iso.startswith(now.strftime("%Y-%m-%dT%H:%M"))


def test_webkit_to_iso_rejects_non_timestamp_looking_ints():
    assert _webkit_to_iso(None) is None
    assert _webkit_to_iso("not a number") is None
    assert _webkit_to_iso(3) is None  # 1601-01-01 plus 3 microseconds — not a plausible date
    assert _webkit_to_iso(WEBKIT_EPOCH_OFFSET_US * 1000) is None  # absurdly far future, out of range


def test_preview_sqlite_tables_lists_tables_and_flags_timestamp_columns(store, chromium_like_db):
    path, _ = chromium_like_db
    res = store.preview_sqlite_tables(path)
    by_name = {t["name"]: t for t in res["tables"]}
    assert set(by_name) == {"urls", "meta"}
    assert by_name["urls"]["row_count"] == 3
    assert by_name["urls"]["likely_timestamp_columns"] == ["last_visit_time"]
    assert by_name["meta"]["likely_timestamp_columns"] == []
    assert {c["name"] for c in by_name["urls"]["columns"]} == {"id", "url", "title", "visit_count", "last_visit_time"}


def test_ingest_sqlite_table_converts_flagged_timestamp_column(store, chromium_like_db):
    path, now = chromium_like_db
    rec = store.ingest_sqlite_table(path, "urls", name="History.urls", build_fts=False,
                                     timestamp_columns=["last_visit_time"])
    assert rec["row_count"] == 3
    coltypes = {c["name"]: c["type"] for c in rec["columns"]}
    assert coltypes["last_visit_time"] == "datetime"
    assert coltypes["id"] == "number"
    assert coltypes["url"] == "text"

    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": [{"column": "id", "dir": "asc"}]})
    rows = store.fetch_rows(view["view_id"], 0, 10)["rows"]
    last_visit = rows[0]["cells"][-1]
    expected_prefix = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    assert last_visit.startswith(expected_prefix)


def test_ingest_sqlite_table_without_conversion_keeps_raw_integer(store, chromium_like_db):
    path, _ = chromium_like_db
    rec = store.ingest_sqlite_table(path, "urls", build_fts=False)  # no timestamp_columns given
    coltypes = {c["name"]: c["type"] for c in rec["columns"]}
    assert coltypes["last_visit_time"] == "number"  # raw WebKit int, not converted


def test_ingest_sqlite_table_handles_blob_and_null(store, tmp_path):
    path = str(tmp_path / "blobs.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE icons (id INTEGER PRIMARY KEY, name TEXT, favicon BLOB, note TEXT)")
    conn.execute("INSERT INTO icons VALUES (1, 'a', ?, NULL)", (b"\x89PNG\x00\x01\x02\x03",))
    conn.commit()
    conn.close()

    rec = store.ingest_sqlite_table(path, "icons", build_fts=False)
    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": []})
    row = store.fetch_rows(view["view_id"], 0, 10)["rows"][0]
    _id, name, favicon, note = row["cells"]
    assert name == "a"
    assert favicon == "<8 bytes>"
    assert note == ""  # NULL -> empty text, same convention as CSV's blank cells


def test_ingest_sqlite_table_raises_for_unknown_table(store, chromium_like_db):
    path, _ = chromium_like_db
    with pytest.raises(ValueError, match="No such table"):
        store.ingest_sqlite_table(path, "does_not_exist")
    # Nothing should have been created for the failed attempt.
    assert store.list_sources() == []


def test_ingest_sqlite_table_source_table_never_mutated(store, chromium_like_db):
    # Invariant from CLAUDE.md, extended to this ingest path too: reading
    # an external .db for import must never write back to it.
    path, _ = chromium_like_db
    before = open(path, "rb").read()
    store.ingest_sqlite_table(path, "urls", build_fts=False, timestamp_columns=["last_visit_time"])
    after = open(path, "rb").read()
    assert before == after


def test_one_unreadable_virtual_table_does_not_hide_the_rest_of_the_file(store, tmp_path):
    """Thunderbird's global-messages-db.sqlite carries an FTS table built
    with its own tokenizer; a SQLite without that module raises "no such
    module" for it — and the preview read every table in one go, so the
    whole file was unimportable. The table is listed with why it can't be
    read, and every other table previews and imports as usual."""
    path = str(tmp_path / "global-messages-db.sqlite")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, subject TEXT)")
    conn.execute("INSERT INTO messages VALUES (1, 'invoice')")
    conn.commit()
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute("INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql) VALUES "
                 "('table','messagesText','messagesText',0,"
                 "'CREATE VIRTUAL TABLE messagesText USING mozvtab(body)')")
    conn.commit()
    conn.close()

    tables = {t["name"]: t for t in store.preview_sqlite_tables(path)["tables"]}
    assert tables["messages"]["row_count"] == 1 and "error" not in tables["messages"]
    assert tables["messagesText"]["row_count"] is None
    assert "mozvtab" in tables["messagesText"]["error"]
    rec = store.ingest_sqlite_table(path, "messages", build_fts=False)
    assert rec["row_count"] == 1


def test_generated_columns_are_imported_too(store, tmp_path):
    """`PRAGMA table_info` omits generated columns, so the import read the
    table without them — a computed column the application shows its own
    users simply wasn't in the case, with nothing saying so."""
    path = str(tmp_path / "app.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, host TEXT, path TEXT, "
                 "url TEXT GENERATED ALWAYS AS ('https://' || host || path) VIRTUAL, "
                 "host_upper TEXT GENERATED ALWAYS AS (upper(host)) STORED)")
    conn.execute("INSERT INTO visits(host, path) VALUES ('example.test', '/a')")
    conn.commit()
    conn.close()

    prev = {t["name"]: t for t in store.preview_sqlite_tables(path)["tables"]}
    assert [c["name"] for c in prev["visits"]["columns"]] == ["id", "host", "path", "url", "host_upper"]
    rec = store.ingest_sqlite_table(path, "visits", build_fts=False)
    assert [c["name"] for c in rec["columns"]] == ["id", "host", "path", "url", "host_upper"]
    with store._reader() as ro:
        row = ro.execute(f'SELECT url, host_upper FROM "{rec["table_name"]}"').fetchone()
    assert tuple(row) == ("https://example.test/a", "EXAMPLE.TEST")


def test_an_fts_tables_hidden_columns_stay_out(store, tmp_path):
    path = str(tmp_path / "fts.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE VIRTUAL TABLE docs USING fts5(body)")
    conn.execute("INSERT INTO docs(body) VALUES ('hello')")
    conn.commit()
    conn.close()
    prev = {t["name"]: t for t in store.preview_sqlite_tables(path)["tables"]}
    assert [c["name"] for c in prev["docs"]["columns"]] == ["body"]


def _wal_evidence(tmp_path, with_wal: bool):
    """A WAL-mode database as a forensic copy carries it: the main file
    plus (optionally) its -wal holding rows not yet checkpointed, and no
    -shm — that one is per-process scratch and copies don't take it."""
    import shutil
    live = tmp_path / "live"
    live.mkdir()
    db = live / "places.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    conn.commit()
    if not with_wal:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    ev = tmp_path / "evidence"
    ev.mkdir()
    shutil.copy(db, ev / "places.sqlite")
    if with_wal:
        shutil.copy(str(db) + "-wal", ev / "places.sqlite-wal")
    conn.close()
    return ev, str(ev / "places.sqlite")


@pytest.mark.parametrize("with_wal", [True, False])
def test_reading_a_wal_database_leaves_nothing_beside_the_evidence(store, tmp_path, with_wal):
    """A read-only open of a WAL-mode database still creates its -shm (and
    -wal, when the copy came without one) beside the file — in the
    analyst's evidence folder. Nothing may be written there; and the rows
    that only exist in the -wal are evidence too, so they must still be
    read."""
    ev, path = _wal_evidence(tmp_path, with_wal)
    before = sorted(p.name for p in ev.iterdir())
    prev = store.preview_sqlite_tables(path)
    assert prev["tables"][0]["row_count"] == 100
    rec = store.ingest_sqlite_table(path, "t", build_fts=False)
    assert rec["row_count"] == 100
    assert sorted(p.name for p in ev.iterdir()) == before


def test_an_uploaded_wal_database_leaves_nothing_in_the_temp_folder(client, tmp_path, monkeypatch):
    """An upload is spooled to a tempfile and removed after — but the
    -wal/-shm the read-only open created beside the spool were not, one
    pair per preview and per import, for good."""
    import tempfile
    _ev, path = _wal_evidence(tmp_path, with_wal=False)
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool))
    with open(path, "rb") as f:
        raw = f.read()
    assert raw[18:20] == b"\x02\x02"                 # the header says WAL
    r = client.post("/api/ingest/sqlite/preview", files={"file": ("places.sqlite", raw)})
    assert r.status_code == 200 and r.json()["tables"][0]["row_count"] == 100
    assert sorted(p.name for p in spool.iterdir()) == []


def _db_with_bad_text(path, n, bad_rowid):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, title TEXT)")
    conn.executemany("INSERT INTO t VALUES (?,?)", [(i, f"row{i}") for i in range(1, n + 1)])
    # Bytes that are not UTF-8, stored as TEXT — what a browser or app DB
    # written by something careless holds.
    conn.execute("UPDATE t SET title = CAST(X'41FF42' AS TEXT) WHERE id=?", (bad_rowid,))
    conn.commit()
    conn.close()


def test_a_text_value_that_is_not_utf8_does_not_abort_the_table(store, tmp_path):
    """sqlite3 raised "Could not decode to UTF-8" mid-read: the batch it
    was in and every row after it were lost, with 20,000 of 30,000 kept."""
    from winnow.store import BATCH

    path = str(tmp_path / "ev.db")
    n = BATCH + 500
    _db_with_bad_text(path, n, BATCH + 10)
    rec = store.ingest_sqlite_table(path, "t", build_fts=False)
    assert rec["row_count"] == n
    assert rec["undecodable_values"] == 1
    v = store.build_view(rec["id"], {"source_id": rec["id"]})
    assert store.fetch_rows(v["view_id"], BATCH + 9, 1)["rows"][0]["cells"] == [str(BATCH + 10), "A\ufffdB"]
    assert store.preview_sqlite_tables(path)["tables"][0]["row_count"] == n


def test_a_failed_sqlite_import_leaves_no_empty_table_behind(store, tmp_path, monkeypatch):
    """A table whose first batch failed was left in the case with 0 rows,
    indistinguishable from a real empty import."""
    from winnow import store as store_mod

    path = str(tmp_path / "ev.db")
    _db_with_bad_text(path, 10, 5)

    def boom(self, *a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store_mod.Store, "_commit_ingest_batch", boom)
    with pytest.raises(sqlite3.OperationalError):
        store.ingest_sqlite_table(path, "t", build_fts=False)
    assert store.list_sources() == []


def test_the_import_job_reports_the_undecodable_values(store, tmp_path):
    path = str(tmp_path / "Cookies.db")
    _db_with_bad_text(path, 50, 5)
    store.start_ingest_job("sqlite", path, options={"tables": [{"table": "t"}]})
    store.wait_for_ingest_job()
    job = store.list_ingest_jobs()[0]
    assert job["status"] == "done", job
    assert [(r["row_count"], r["undecodable_values"]) for r in job["result"]] == [(50, 1)]
