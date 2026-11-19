"""A full disk, said as one (winnow/diskspace.py).

Reported from the field: the analyst's drive filled up and a table import
"failed silently". Reproduced on a small tmpfs before any of this was
written, both ways a full disk stops an import:

- the TEMP folder fills while a browser upload is still arriving: FastAPI
  answered "There was an error parsing the body" (400), logged nothing,
  and the page showed that in a toast for six seconds;
- the CASE's drive fills mid-import: the job failed with "Line 60,001:
  database or disk is full — import stopped there; the 40,000 rows before
  it were kept", blaming a line of the file, and the partial table it
  left looked like any other.

These pin what each now says. The disk is faked — a commit that raises
SQLite's own SQLITE_FULL message, a write that raises ENOSPC — because a
filesystem small enough to fill in a test is not something CI can mount;
the messages are the ones a real full tmpfs produced."""

from __future__ import annotations

import errno
import io
import json
import os
import sqlite3
from collections import namedtuple

import pytest
from fastapi import HTTPException

from winnow import diskspace
from winnow import log as wlog

HEADERS = {"X-Timeline-Lite-Client": "1"}
FULL = "database or disk is full"

Usage = namedtuple("Usage", "total used free")


def _enospc():
    return OSError(errno.ENOSPC, "No space left on device")


def _rows_csv(path, n):
    with open(path, "w", encoding="utf-8") as f:
        f.write("Timestamp,EventId,User\n")
        for i in range(n):
            f.write(f"2024-01-05 13:{i % 60:02d}:00,{4624 + i % 3},user{i}\n")
    return str(path)


@pytest.fixture
def small_batches(monkeypatch):
    """Batches of 10, so "the disk filled after the first commit" takes a
    30-row file rather than a 40,000-row one."""
    from winnow import store as store_mod
    monkeypatch.setattr(store_mod, "BATCH", 10)
    return 10


@pytest.fixture
def disk_fills(store, monkeypatch):
    """disk_fills(after=n): the case's disk fills once n ingest batches
    have committed. Every commit after that raises SQLite's own message,
    including the reader's attempt to save its pending batch — as a real
    full disk does."""
    def arm(after: int):
        real = store._commit_ingest_batch
        calls = {"n": 0}

        def commit(*a, **kw):
            calls["n"] += 1
            if calls["n"] > after:
                raise sqlite3.OperationalError(FULL)
            return real(*a, **kw)

        monkeypatch.setattr(store, "_commit_ingest_batch", commit)
    return arm


@pytest.fixture
def fresh_log():
    wlog.reset()
    yield
    wlog.reset()


def _errors():
    return [e["message"] for e in wlog.entries() if e["level"] == "error"]


# ------------------------------------------------------------- recognising one


@pytest.mark.parametrize("exc", [
    _enospc(),
    OSError(getattr(errno, "EDQUOT", errno.ENOSPC), "Disk quota exceeded"),
    sqlite3.OperationalError(FULL),
    diskspace.DiskFullError("nothing was imported", "import the file again"),
])
def test_the_errors_a_full_disk_raises_are_recognised(exc):
    assert diskspace.is_disk_full(exc)


def test_a_windows_disk_full_code_is_recognised_without_an_errno():
    e = OSError(0, "There is not enough space on the disk")
    e.winerror = 112
    assert diskspace.is_disk_full(e)


@pytest.mark.parametrize("exc", [
    OSError(errno.ENOENT, "No such file or directory"),
    sqlite3.OperationalError("disk I/O error"),   # a failing disk is not a full one
    sqlite3.OperationalError("database is locked"),
    ValueError("Line 3: field larger than field limit"),
    None,
])
def test_other_errors_are_not(exc):
    assert not diskspace.is_disk_full(exc)


def test_found_through_the_readers_wrapper_and_through_a_reraise():
    """The readers wrap (`raise ValueError(...) from e`), FastAPI wraps a
    failed form parse the same way, and the upload routes re-raise inside
    their except (the implicit context). Each has to be seen through."""
    cause = sqlite3.OperationalError(FULL)
    wrapped = ValueError(f"Line 9: {cause}")
    wrapped.__cause__ = cause
    assert diskspace.is_disk_full(wrapped)
    assert diskspace.root_cause(wrapped) is cause

    try:
        try:
            raise _enospc()
        except OSError as e:
            raise HTTPException(400, str(e))
    except HTTPException as http:
        assert diskspace.is_disk_full(http)


def test_raise_from_none_hides_the_context():
    try:
        try:
            raise _enospc()
        except OSError:
            raise ValueError("unrelated") from None
    except ValueError as e:
        assert not diskspace.is_disk_full(e)


# ------------------------------------------------------------- saying where


def test_free_space_names_each_drive_emptiest_first(monkeypatch, tmp_path):
    case, temp = tmp_path / "case", tmp_path / "temp"
    case.mkdir()
    temp.mkdir()
    free = {str(case): 0, str(temp): 13 * 1024 ** 3}
    monkeypatch.setattr(diskspace.shutil, "disk_usage", lambda p: Usage(1, 1, free[str(p)]))
    monkeypatch.setattr(diskspace, "_drive_key", lambda p: p)   # two drives
    said = diskspace.free_space([("the temp folder", str(temp)), ("the case", str(case))])
    assert said == (f"The drive holding the case ({case}) has 0 B free; "
                    f"the one holding the temp folder ({temp}) has 13.0 GB.")


def test_folders_on_one_drive_share_a_clause(monkeypatch, tmp_path):
    case, temp = tmp_path / "case", tmp_path / "temp"
    case.mkdir()
    temp.mkdir()
    monkeypatch.setattr(diskspace.shutil, "disk_usage", lambda p: Usage(1, 1, 4096))
    monkeypatch.setattr(diskspace, "_drive_key", lambda p: "C:")
    said = diskspace.free_space([("the case", str(case)), ("the temp folder", str(temp)),
                                 ("the case", str(case))])
    assert said == f"The drive holding the case ({case}) and the temp folder ({temp}) has 4.0 KB free."


def test_a_folder_that_is_gone_is_measured_on_its_drive(tmp_path):
    said = diskspace.free_space([("the case", str(tmp_path / "gone" / "deeper"))])
    assert said.startswith(f"The drive holding the case ({tmp_path / 'gone' / 'deeper'}) has ")


@pytest.mark.parametrize("n, text", [
    (0, "0 B"), (512, "512 B"), (4096, "4.0 KB"), (int(37.2 * 1024 ** 2), "37.2 MB"),
    (int(1.4 * 1024 ** 3), "1.4 GB"), (250 * 1024 ** 3, "250 GB"),
])
def test_sizes_read_the_way_an_analyst_reads_them(n, text):
    assert diskspace.format_size(n) == text


# ------------------------------------------------------------- import jobs


def test_a_csv_job_the_disk_stops_says_so_and_what_it_kept(store, tmp_path, small_batches, disk_fills, fresh_log):
    path = _rows_csv(tmp_path / "big.csv", 35)
    disk_fills(after=2)
    job = store.start_ingest_job("csv", path, name="big.csv")
    done = store.wait_for_ingest_job(job["job_id"], timeout=30)

    assert done["status"] == "error"
    assert done["error_kind"] == "disk_full"
    msg = done["error"]
    assert msg.startswith("Out of disk space: the import stopped after 20 rows, "
                          "which were kept as a partial table. ")
    case_dir = os.path.dirname(os.path.abspath(store.path))
    assert f"the case ({case_dir})" in msg
    assert msg.endswith("Free up space, then delete that table and import the file again.")
    # Not blamed on a line of the file: the reader was a batch past the
    # commit that failed.
    assert "Line " not in msg

    src = store.list_sources()
    assert [s["row_count"] for s in src] == [20]
    # The record, with SQLite's own words for whoever reads the log next.
    assert any(m.startswith("Import failed: big.csv (csv) — Out of disk space")
               and m.endswith(f"[OperationalError: {FULL}]") for m in _errors())


def test_a_job_that_kept_nothing_leaves_no_table(store, tmp_path, small_batches, disk_fills):
    path = _rows_csv(tmp_path / "big.csv", 35)
    disk_fills(after=0)
    done = store.wait_for_ingest_job(store.start_ingest_job("csv", path)["job_id"], timeout=30)
    assert done["error_kind"] == "disk_full"
    assert done["error"].startswith("Out of disk space: nothing was imported. ")
    assert done["error"].endswith("Free up space, then import the file again.")
    assert store.list_sources() == []


def test_a_json_job_counts_records(store, tmp_path, small_batches, disk_fills):
    p = tmp_path / "events.jsonl"
    p.write_text("".join(json.dumps({"id": i, "who": f"u{i}"}) + "\n" for i in range(25)), encoding="utf-8")
    disk_fills(after=1)
    done = store.wait_for_ingest_job(store.start_ingest_job("json", str(p))["job_id"], timeout=30)
    assert done["error_kind"] == "disk_full"
    assert done["error"].startswith("Out of disk space: the import stopped after 10 records, "
                                    "which were kept as a partial table.")


def test_a_sqlite_job_says_which_tables_it_finished(store, tmp_path, small_batches, monkeypatch):
    """A multi-table job: the first table lands, the disk fills on the
    second. The message is the second table's, plus the first one kept."""
    db = tmp_path / "evidence.sqlite"
    con = sqlite3.connect(db)
    for t in ("first", "second"):
        con.execute(f"CREATE TABLE {t} (a TEXT, b TEXT)")
        con.executemany(f"INSERT INTO {t} VALUES (?, ?)", [(str(i), "x") for i in range(25)])
    con.commit()
    con.close()

    real = store._commit_ingest_batch
    calls = {"n": 0}

    def commit(*a, **kw):   # three batches for `first`, then one for `second`
        calls["n"] += 1
        if calls["n"] > 4:
            raise sqlite3.OperationalError(FULL)
        return real(*a, **kw)

    monkeypatch.setattr(store, "_commit_ingest_batch", commit)
    job = store.start_ingest_job("sqlite", str(db), options={"tables": [{"table": "first"}, {"table": "second"}]})
    done = store.wait_for_ingest_job(job["job_id"], timeout=30)
    assert done["error_kind"] == "disk_full"
    assert done["error"].startswith(
        "Out of disk space: the import stopped after 10 rows, which were kept as a partial table "
        "(the 1 table it finished before that was kept). ")
    assert sorted(s["row_count"] for s in store.list_sources()) == [10, 25]


def test_any_other_failure_is_reported_as_it_was(store, tmp_path, small_batches, monkeypatch):
    path = _rows_csv(tmp_path / "big.csv", 35)
    real = store._commit_ingest_batch
    calls = {"n": 0}

    def commit(*a, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real(*a, **kw)

    monkeypatch.setattr(store, "_commit_ingest_batch", commit)
    done = store.wait_for_ingest_job(store.start_ingest_job("csv", path)["job_id"], timeout=30)
    assert done["error_kind"] is None
    assert "disk I/O error" in done["error"] and "Out of disk space" not in done["error"]


def test_a_search_index_the_disk_stops_is_an_error_not_a_warning(store, write_csv, monkeypatch, fresh_log):
    """It used to be a warning: the import's toast said it worked, the
    indexing row went away, and the badge stayed dark — while the disk
    that stopped the index would stop the next tag write too."""
    def full(source_id):
        raise sqlite3.OperationalError(FULL)

    monkeypatch.setattr(store, "build_fts", full)
    path = write_csv([["a", "b"], ["1", "2"]])
    src = store.ingest_csv(path, name="t.csv")
    store.wait_for_fts(src["id"], timeout=30)
    errs = [m for m in _errors() if m.startswith(f"Search index for table {src['id']} not built")]
    assert errs and "out of disk space" in errs[0] and "The drive holding the case (" in errs[0]


def test_import_indexes_stop_at_the_first_full_disk(store, write_csv, monkeypatch, fresh_log):
    """One line that says why, not one warning per column that would all
    have failed the same way."""
    src = store.ingest_csv(write_csv([["Timestamp", "EventId"], ["2024-01-05 13:22:01", "4624"]]),
                           name="t.csv", build_fts=False)
    with store.lock, store.db:
        for (name,) in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (f"src_{src['id']}",)).fetchall():
            store.db.execute(f'DROP INDEX "{name}"')

    class FullDb:
        def __init__(self, real):
            self.real = real

        def execute(self, sql, *a):
            if sql.startswith("CREATE INDEX"):
                raise sqlite3.OperationalError(FULL)
            return self.real.execute(sql, *a)

        def __enter__(self):
            return self.real.__enter__()

        def __exit__(self, *a):
            return self.real.__exit__(*a)

    monkeypatch.setattr(store, "_shape_index_columns", lambda src: ["EventId", "Timestamp"])
    real_db = store.db
    monkeypatch.setattr(store, "db", FullDb(real_db))
    try:
        store._build_import_indexes(src["id"])
    finally:
        monkeypatch.setattr(store, "db", real_db)
    msgs = [e for e in wlog.entries() if "t.csv" in e["message"]]
    assert len(msgs) == 1 and msgs[0]["level"] == "error"
    assert "imported, but out of disk space for its indexes" in msgs[0]["message"]


# ------------------------------------------------------------- the HTTP layer


def test_an_upload_the_temp_folder_cannot_hold_is_a_507_that_says_so(client, monkeypatch, fresh_log):
    """The case that reached the analyst as "There was an error parsing
    the body": the form parse writes the upload to a temp file before the
    route runs, and FastAPI drops the OSError into __cause__."""
    from starlette.datastructures import UploadFile

    async def write(self, data):
        raise _enospc()

    monkeypatch.setattr(UploadFile, "write", write)
    r = client.post("/api/ingest/jobs/upload", data={"kind": "csv"},
                    files={"file": ("big.csv", io.BytesIO(b"a,b\n1,2\n" * 1000), "text/csv")})
    assert r.status_code == 507
    detail = r.json()["detail"]
    assert detail.startswith("Out of disk space: the file (about ")
    assert "could not be received, so nothing was imported." in detail
    assert "The drive holding the " in detail and "the temp folder (" in detail
    assert detail.endswith("Free up space, then import it again.")
    assert "There was an error parsing the body" not in detail
    # FastAPI's parse failure logged nothing at all.
    assert any(m.startswith("POST /api/ingest/jobs/upload — Out of disk space")
               and m.endswith("[OSError: [Errno 28] No space left on device]") for m in _errors())


def test_an_upload_whose_spool_copy_fills_the_disk(client, monkeypatch):
    """The route's own copy into its spool: "[Errno 28] No space left on
    device" was the whole message."""
    import server

    class FullFile(io.BytesIO):
        def write(self, b):
            raise _enospc()

    monkeypatch.setattr(server, "open", lambda *a, **kw: FullFile(), raising=False)
    r = client.post("/api/ingest/jobs/upload", data={"kind": "csv"},
                    files={"file": ("big.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")})
    assert r.status_code == 507
    assert r.json()["detail"].startswith("Out of disk space: the file (about ")


def test_a_synchronous_import_keeps_the_readers_account(client, store, small_batches, disk_fills):
    """A route that turned the reader's error into a 400 itself: the
    message is the reader's, what it kept and all."""
    disk_fills(after=1)
    body = "".join(f"r{i},x\n" for i in range(25))
    r = client.post("/api/ingest/upload", files={"file": ("big.csv", io.BytesIO(("a,b\n" + body).encode()), "text/csv")})
    assert r.status_code == 507
    assert r.json()["detail"].startswith(
        "Out of disk space: the import stopped after 10 rows, which were kept as a partial table. ")


def test_any_route_that_hits_a_full_disk(client, store, monkeypatch):
    """The catch-all: a 507 that says so, not "Internal error: database or
    disk is full"."""
    import server
    from fastapi.testclient import TestClient

    def full(*a, **kw):
        raise sqlite3.OperationalError(FULL)

    monkeypatch.setattr(store, "list_tags", full)
    # As test_api_routes.py's catch-all tests do: the test client would
    # otherwise re-raise what the handler already answered.
    loose = TestClient(server.app, headers=HEADERS, raise_server_exceptions=False)
    r = loose.get("/api/tags")
    assert r.status_code == 507
    assert r.json()["detail"].startswith("Out of disk space: that could not be finished. ")


def test_other_http_errors_are_untouched(client, tmp_path):
    r = client.post("/api/ingest/jobs/path", json={"path": str(tmp_path / "missing.csv")})
    assert r.status_code == 400
    assert r.json() == {"detail": f"No file at {tmp_path / 'missing.csv'}"}
    # And a body that really is malformed still gets FastAPI's own answer.
    r = client.post("/api/ingest/jobs/upload", content=b"--x\r\nnot a form",
                    headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code in (400, 422)
    assert "Out of disk space" not in r.text
