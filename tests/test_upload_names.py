"""A file uploaded from the browser imports as the file the analyst picked.

The upload is spooled to a temp file before it is read, and two things
used to come from that spool instead of from the file: the name a SQLite
table or a workbook sheet is given (built from the file name, so an
uploaded History's `urls` became `tmp3z2vauq7.urls` while the same file
added by path became `History.urls`), and, for every kind, the path the
table records as where it came from — a temp file deleted when the
import ended, which a session then went looking for.

Driven through the upload routes, since the spool is theirs.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile

import pytest
from openpyxl import Workbook

from test_plaso_import import make_old_plaso
from winnow.store import DEFAULT_TAGS, Store


@pytest.fixture
def systmp(tmp_path, monkeypatch):
    """A temp folder of the test's own, so what an upload leaves behind
    (or the spool path a table records) can be seen."""
    d = tmp_path / "systmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


def _history(path) -> None:
    """A Chromium-shaped History database: no extension, two tables."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT)")
    c.executemany("INSERT INTO urls (url) VALUES (?)", [("https://a.test/",), ("https://b.test/",)])
    c.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER)")
    c.execute("INSERT INTO visits (url) VALUES (1)")
    c.commit()
    c.close()


def _upload(client, store, name: str, data: bytes, kind: str, **form) -> dict:
    r = client.post("/api/ingest/jobs/upload", files={"file": (name, data)},
                    data={"kind": kind, "build_fts": "false", **form})
    assert r.status_code == 200, r.text
    return store.wait_for_ingest_job(r.json()["job_id"], timeout=30)


def _by_path(client, store, path, **body) -> dict:
    r = client.post("/api/ingest/jobs/path", json={"path": str(path), "build_fts": False, **body})
    assert r.status_code == 200, r.text
    return store.wait_for_ingest_job(r.json()["job_id"], timeout=30)


def test_an_uploaded_sqlite_file_names_its_tables_like_a_path_import(client, store, tmp_path, systmp):
    db = tmp_path / "evidence" / "History"
    _history(db)
    tables = [{"table": "urls"}, {"table": "visits"}]
    by_path = _by_path(client, store, db, kind="sqlite", tables=tables)
    up = _upload(client, store, "History", db.read_bytes(), "sqlite", tables=json.dumps(tables))
    assert up["status"] == "done", up["error"]

    path_names = [store.get_source(i)["name"] for i in by_path["source_ids"]]
    up_srcs = [store.get_source(i) for i in up["source_ids"]]
    assert path_names == ["History.urls", "History.visits"]
    assert [s["name"] for s in up_srcs] == path_names
    # Where they came from: the file the analyst picked, not the spool.
    assert [s["path"] for s in up_srcs] == ["History", "History"]
    assert os.listdir(systmp) == []          # spool and its folder both gone


def test_an_uploaded_workbook_names_its_sheets_like_a_path_import(client, store, tmp_path, systmp):
    wb = Workbook()
    wb.active.title = "Sheet1"
    wb.active.append(["Host", "User"])
    wb.active.append(["h1", "alice"])
    book = tmp_path / "Book1.xlsx"
    wb.save(book)
    tables = json.dumps([{"table": "Sheet1"}])
    by_path = _by_path(client, store, book, kind="xlsx", tables=[{"table": "Sheet1"}])
    up = _upload(client, store, "Book1.xlsx", book.read_bytes(), "xlsx", tables=tables)
    assert up["status"] == "done", up["error"]
    src = store.get_source(up["source_ids"][0])
    assert src["name"] == store.get_source(by_path["source_ids"][0])["name"] == "Book1.Sheet1"
    assert src["path"] == "Book1.xlsx"
    assert os.listdir(systmp) == []


@pytest.mark.parametrize("name,kind,data", [
    ("logons.csv", "csv", b"Host,User\nh1,alice\nh2,bob\n"),
    ("auth.log.1", "text", b"Jan  1 00:00:01 h sshd[1]: Accepted\nJan  1 00:00:02 h sshd[1]: Closed\n"),
    ("events.jsonl", "json", b'{"a": 1}\n{"a": 2}\n'),
])
def test_every_upload_records_the_file_it_came_from(client, store, systmp, name, kind, data):
    up = _upload(client, store, name, data, kind)
    assert up["status"] == "done", up["error"]
    src = store.get_source(up["source_ids"][0])
    assert src["name"] == name
    assert src["path"] == name
    assert os.listdir(systmp) == []


def test_an_uploaded_plaso_file_records_the_file_it_came_from(client, store, tmp_path, systmp):
    plaso = tmp_path / "run.plaso"
    make_old_plaso(plaso)
    up = _upload(client, store, "run.plaso", plaso.read_bytes(), "plaso")
    assert up["status"] == "done", up["error"]
    src = store.get_source(up["source_ids"][0])
    assert (src["name"], src["path"]) == ("run.plaso", "run.plaso")
    assert os.listdir(systmp) == []


def test_rows_kept_from_a_failed_upload_record_the_file_too(client, store, systmp):
    """A CSV that breaks mid-file keeps what it committed — and that table
    is as much the upload's as a finished one."""
    rows = "a,b\n" + "".join(f"r{i},x\n" for i in range(25_000))
    before = csv.field_size_limit(131_072)
    try:
        up = _upload(client, store, "poison.csv", (rows + "bad," + "x" * 200_000 + "\n").encode(), "csv")
    finally:
        csv.field_size_limit(before)
    assert up["status"] == "error"
    [src] = store.list_sources()
    assert src["row_count"] == 25_000
    assert store.get_source(src["id"])["path"] == "poison.csv"
    assert os.listdir(systmp) == []


def test_a_session_names_the_uploaded_file_not_a_deleted_spool(client, store, tmp_path, systmp):
    """The symptom the analyst met: loading the session somewhere the
    table isn't sent them to a temp file that never existed for them."""
    db = tmp_path / "evidence" / "History"
    _history(db)
    up = _upload(client, store, "History", db.read_bytes(), "sqlite",
                 tables=json.dumps([{"table": "urls"}]))
    sid = up["source_ids"][0]
    tag = store.list_tags()[0]["id"]
    store.set_tags(sid, [1], tag, True)
    doc = store.export_case_session()
    assert doc["sources"][0]["source"]["path"] == "History"

    other = Store(str(tmp_path / "other.db-winnow"), default_tags=DEFAULT_TAGS)
    try:
        res = other.import_case_session(doc)
        assert res["sources_restored"] == 0
        [why] = res["warnings"]
        assert "History.urls" in why and str(systmp) not in why and "tmp" not in why
        # Opened from where it lives, the same table takes the session.
        other.ingest_sqlite_table(str(db), "urls", build_fts=False)
        assert other.import_case_session(doc)["sources_restored"] == 1
    finally:
        other.close()


def test_the_older_upload_routes_record_the_file_too(client, store, tmp_path, systmp):
    db = tmp_path / "evidence" / "History"
    _history(db)
    r = client.post("/api/ingest/sqlite/upload", files={"file": ("History", db.read_bytes())},
                    data={"table": "urls", "build_fts": "false"})
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["path"]) == ("History.urls", "History")
    r = client.post("/api/ingest/upload", files={"file": ("logons.csv", b"A,B\n1,2\n")},
                    data={"build_fts": "false"})
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["path"]) == ("logons.csv", "logons.csv")
    r = client.post("/api/ingest/json/upload", files={"file": ("events.jsonl", b'{"a": 1}\n')},
                    data={"build_fts": "false"})
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["path"]) == ("events.jsonl", "events.jsonl")
    assert sorted(s["path"] for s in store.list_sources()) == ["History", "events.jsonl", "logons.csv"]
    assert os.listdir(systmp) == []
