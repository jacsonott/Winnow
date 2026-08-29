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
