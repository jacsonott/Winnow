"""Background ingest jobs (Store.start_ingest_job and friends): lifecycle,
progress fields, the cancel-drops-the-partial-source contract, the
multi-table SQLite job shape, and the HTTP layer. See CLAUDE.md — these
exist so a 50 GB import isn't one opaque multi-minute POST."""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from winnow.store import BATCH, IngestCancelled, Store, DEFAULT_TAGS

from conftest import STANDARD_ROWS


def _big_rows(n):
    yield ["Timestamp", "EventId", "Details"]
    for i in range(n):
        yield [f"2024-01-05 13:{i % 60:02d}:{i % 60:02d}", str(4624 + (i % 3)), f"row {i}"]


def test_csv_job_lifecycle(store, write_csv):
    path = write_csv(STANDARD_ROWS)
    job = store.start_ingest_job("csv", path, name="std.csv")
    assert job["status"] in ("queued", "running")
    assert job["unit"] == "bytes"
    assert job["units_total"] == os.path.getsize(path)

    done = store.wait_for_ingest_job(job["job_id"], timeout=30)
    assert done["status"] == "done"
    assert done["rows_done"] == 4
    assert len(done["source_ids"]) == 1
    assert done["result"][0]["row_count"] == 4
    assert done["error"] is None

    src = store.get_source(done["source_ids"][0])
    assert src["name"] == "std.csv"
    assert src["row_count"] == 4


def test_csv_job_reports_byte_progress(store, write_csv):
    # More than one BATCH so the progress callback actually fires mid-file.
    path = write_csv(list(_big_rows(BATCH + 100)))
    job = store.start_ingest_job("csv", path)
    done = store.wait_for_ingest_job(job["job_id"], timeout=60)
    assert done["status"] == "done"
    assert done["rows_done"] == BATCH + 100
    # units are bytes for CSV; the callback fires per committed batch, so
    # units_done is at least one batch's worth of the file, and never more
    # than the file itself.
    assert 0 < done["units_done"] <= done["units_total"] == os.path.getsize(path)


def test_cancel_mid_ingest_drops_partial_source(store, write_csv):
    """The contract: an explicit cancel *discards* the partial import
    (unlike a mid-file error, which keeps what committed). Driven through
    ingest_csv's own cancel hook so the cancellation point — after the
    first committed batch, before the final one — is deterministic, not a
    race against a background thread."""
    path = write_csv(list(_big_rows(BATCH + 50)))
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 1  # first batch commits, the final batch cancels

    with pytest.raises(IngestCancelled):
        store.ingest_csv(path, name="doomed.csv", cancel=cancel)
    assert store.list_sources() == []
    # And the backing table is gone too, not just the sources row.
    tables = [r[0] for r in store.db.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'src_%'")]
    assert tables == []


def test_cancel_queued_job(store, write_csv):
    # Saturate the concurrency cap with two slow-ish jobs, then verify a
    # third can be cancelled while still queued.
    big = write_csv(list(_big_rows(BATCH * 2)), name="big.csv")
    j1 = store.start_ingest_job("csv", big)
    j2 = store.start_ingest_job("csv", big, name="second.csv")
    j3 = store.start_ingest_job("csv", big, name="third.csv")
    assert store.cancel_ingest_job(j3["job_id"]) is True
    store.wait_for_ingest_job(timeout=120)
    s3 = next(j for j in store.list_ingest_jobs() if j["job_id"] == j3["job_id"])
    # Either it never started (queued-cancel) or it started and the per-batch
    # check caught it; both must end 'cancelled' with no source left behind.
    assert s3["status"] == "cancelled"
    assert s3["source_ids"] == []
    names = [s["name"] for s in store.list_sources()]
    assert "third.csv" not in names
    for j in (j1, j2):
        st = store.wait_for_ingest_job(j["job_id"], timeout=120)
        assert st["status"] == "done"


def test_job_error_reported(store, tmp_path):
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    job = store.start_ingest_job("csv", str(empty))
    done = store.wait_for_ingest_job(job["job_id"], timeout=30)
    assert done["status"] == "error"
    assert "empty" in done["error"].lower()


def test_json_job(store, tmp_path):
    p = tmp_path / "recs.jsonl"
    p.write_text('{"a": "1", "b": "x"}\n{"a": "2"}\n')
    job = store.start_ingest_job("json", str(p))
    done = store.wait_for_ingest_job(job["job_id"], timeout=30)
    assert done["status"] == "done"
    assert done["result"][0]["row_count"] == 2
    assert done["unit"] == "records"


def test_sqlite_job_multi_table_single_file(store, tmp_path):
    dbp = tmp_path / "ext.db"
    conn = sqlite3.connect(dbp)
    conn.execute("CREATE TABLE alpha (x TEXT)")
    conn.execute("CREATE TABLE beta (y TEXT)")
    conn.executemany("INSERT INTO alpha VALUES (?)", [("a",), ("b",)])
    conn.executemany("INSERT INTO beta VALUES (?)", [("c",)])
    conn.commit()
    conn.close()

    job = store.start_ingest_job(
        "sqlite", str(dbp),
        options={"tables": [{"table": "alpha"}, {"table": "beta"}], "build_fts": False},
    )
    assert job["tables_total"] == 2
    done = store.wait_for_ingest_job(job["job_id"], timeout=30)
    assert done["status"] == "done"
    assert done["tables_done"] == 2
    assert len(done["source_ids"]) == 2
    counts = sorted(s["row_count"] for s in store.list_sources())
    assert counts == [1, 2]


def test_delete_after_removes_spool(store, write_csv, tmp_path):
    spool = write_csv(STANDARD_ROWS, name="spool.csv")
    job = store.start_ingest_job("csv", spool, name="orig.csv", delete_after=True)
    done = store.wait_for_ingest_job(job["job_id"], timeout=30)
    assert done["status"] == "done"
    assert not os.path.exists(spool)


def test_close_with_running_job_returns(case_path, write_csv):
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    try:
        big = write_csv(list(_big_rows(BATCH * 3)), name="huge.csv")
        s.start_ingest_job("csv", big)
        time.sleep(0.05)  # let the worker get going
        t0 = time.time()
        s.close()  # must cancel the job and return, not hang or crash
        assert time.time() - t0 < 30
    finally:
        # close() is not idempotent-guarded here; a second close would raise.
        pass


# ------------------------------------------------------------------ HTTP


def test_job_upload_endpoint_roundtrip(client, store):
    body = "Timestamp,EventId\n2024-01-05 13:00:00,4624\n2024-01-05 13:01:00,4625\n"
    r = client.post(
        "/api/ingest/jobs/upload",
        files={"file": ("up.csv", body.encode(), "text/csv")},
        data={"kind": "csv"},
    )
    assert r.status_code == 200
    job = r.json()
    assert job["status"] in ("queued", "running")
    store.wait_for_ingest_job(job["job_id"], timeout=30)

    r = client.get("/api/ingest/jobs")
    assert r.status_code == 200
    row = next(j for j in r.json()["jobs"] if j["job_id"] == job["job_id"])
    assert row["status"] == "done"
    assert row["name"] == "up.csv"
    assert row["result"][0]["row_count"] == 2
    # The snapshot must not leak the server-side spool path.
    assert "path" not in row

    srcs = client.get("/api/sources").json()
    assert any(s["name"] == "up.csv" for s in srcs)


def test_job_path_endpoint_autodetects_json(client, store, tmp_path):
    p = tmp_path / "auto.jsonl"
    p.write_text('{"k": "v"}\n')
    r = client.post("/api/ingest/jobs/path", json={"path": str(p)})
    assert r.status_code == 200
    job = r.json()
    assert job["kind"] == "json"
    done = store.wait_for_ingest_job(job["job_id"], timeout=30)
    assert done["status"] == "done"


def test_job_cancel_endpoint_contract(client, store, write_csv):
    path = write_csv(STANDARD_ROWS)
    job = store.start_ingest_job("csv", path)
    store.wait_for_ingest_job(job["job_id"], timeout=30)
    # Cancelling a finished job is a no-op, reported as such — not an error.
    r = client.post(f"/api/ingest/jobs/{job['job_id']}/cancel")
    assert r.status_code == 200
    assert r.json() == {"cancelled": False}
    r = client.post("/api/ingest/jobs/999999/cancel")
    assert r.json() == {"cancelled": False}


# --------------------------------------------- server-disk (no-copy) path


def test_preview_path_csv_and_sqlite(client, write_csv, tmp_path):
    csv_path = write_csv([["Name", "Val"], ["a", "1"]], name="p.csv")
    r = client.post("/api/ingest/preview/path", json={"path": csv_path})
    assert r.status_code == 200
    assert r.json()["columns"] == ["Name", "Val"]

    dbp = tmp_path / "ext.db"
    conn = sqlite3.connect(dbp)
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('v')")
    conn.commit()
    conn.close()
    r = client.post("/api/ingest/preview/path", json={"path": str(dbp)})
    assert r.status_code == 200
    assert [t["name"] for t in r.json()["tables"]] == ["t"]


def test_preview_path_json(client, tmp_path):
    p = tmp_path / "recs.jsonl"
    p.write_text('{"a": "1"}\n{"a": "2"}\n')
    r = client.post("/api/ingest/preview/path", json={"path": str(p)})
    assert r.status_code == 200
    assert "a" in r.json()["columns"]


def test_sqlite_job_by_path(client, store, tmp_path):
    dbp = tmp_path / "ext2.db"
    conn = sqlite3.connect(dbp)
    conn.execute("CREATE TABLE alpha (x TEXT)")
    conn.execute("INSERT INTO alpha VALUES ('a')")
    conn.commit()
    conn.close()

    # No tables picked -> refused up front, not a doomed job.
    r = client.post("/api/ingest/jobs/path", json={"path": str(dbp)})
    assert r.status_code == 400

    r = client.post("/api/ingest/jobs/path",
                    json={"path": str(dbp), "tables": [{"table": "alpha"}], "build_fts": False})
    assert r.status_code == 200
    job = r.json()
    assert job["kind"] == "sqlite"
    done = store.wait_for_ingest_job(job["job_id"], timeout=30)
    assert done["status"] == "done"
    assert store.list_sources()[0]["row_count"] == 1
    # Path-based: the source file is read in place, never spooled/deleted.
    assert os.path.exists(dbp)


# --------------------------------------- same-host invisible path recovery


def test_resolve_local_route_is_gone(client):
    """The fingerprint resolver is deliberately removed — imports are now
    exactly what they look like (a picked path reads in place, an upload
    uploads), never a content-match guess that worked only as often as it
    worked. This pin keeps the route from quietly returning."""
    r = client.post("/api/ingest/resolve_local",
                    files={"head": ("h", b""), "tail": ("t", b"")},
                    data={"name": "x.csv", "size": "1", "mtime_ms": "1"})
    assert r.status_code in (404, 405)
