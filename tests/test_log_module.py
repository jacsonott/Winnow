"""winnow/log.py — the ring the UI shows from Case ▾ → Log — and the
things that now write to it: ingest jobs, exports, and the browser."""

from __future__ import annotations

import csv

import pytest
from fastapi.testclient import TestClient

import server
from winnow import log as wlog
from winnow.store import Store


@pytest.fixture(autouse=True)
def _clean_log():
    wlog.reset()
    yield
    wlog.reset()


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "case.db-winnow"))
    yield s
    s.close()


@pytest.fixture
def client(store):
    server.STORE = store
    server.ALLOWED_HOSTS = server.ALLOWED_HOSTS | {"testserver"}
    return TestClient(server.app, headers={"X-Timeline-Lite-Client": "1"})


def _csv(path, rows, header=("A", "B")):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return str(path)


# ---------------------------------------------------------------- the ring

def test_levels_normalise_and_only_errors_move_error_seq():
    assert wlog.record("info", "a") == 1
    assert wlog.record("WARNING", "b") == 2
    assert wlog.error_seq() == 0
    assert wlog.record("error", "c") == 3
    assert wlog.error_seq() == 3
    wlog.record("nonsense", "d")
    snap = wlog.snapshot()
    assert [e["level"] for e in snap["entries"]] == ["info", "warn", "error", "info"]
    assert snap["seq"] == 4 and snap["error_seq"] == 3


def test_the_ring_is_bounded_and_seq_keeps_counting():
    for i in range(wlog.RING + 100):
        wlog.record("info", str(i))
    snap = wlog.snapshot()
    assert len(snap["entries"]) == wlog.RING
    assert snap["entries"][0]["seq"] == 101 and snap["seq"] == wlog.RING + 100


def test_the_route_returns_the_snapshot_shape(client):
    wlog.record("error", "boom")
    data = client.get("/api/log").json()
    assert set(data) == {"entries", "seq", "error_seq"}
    assert data["error_seq"] == data["seq"] == 1
    assert data["entries"][0]["message"] == "boom"


def test_record_log_alias_still_works(client):
    server.record_log("error", "legacy")
    assert wlog.snapshot()["entries"][-1]["message"] == "legacy"


# ---------------------------------------------------------------- writers

def test_the_client_route_can_warn_but_never_error(client):
    r = client.post("/api/log/client", json={"level": "error", "message": "  poll died  "})
    assert r.status_code == 200
    (entry,) = wlog.entries()
    assert entry["level"] == "warn" and entry["message"] == "[client] poll died"
    assert wlog.error_seq() == 0, "a client message must not light the badge"
    client.post("/api/log/client", json={"level": "info", "message": "x" * 2000})
    assert len(wlog.entries()[-1]["message"]) == len("[client] ") + 500


def test_an_ingest_job_logs_queued_and_finished(store, tmp_path):
    path = _csv(tmp_path / "hosts.csv", [["a", "b"], ["c", "d"], ["e", "f"]])
    job = store.start_ingest_job("csv", path, name="hosts.csv")
    store.wait_for_ingest_job(job["job_id"], timeout=30)
    msgs = [e["message"] for e in wlog.entries()]
    assert any(m.startswith("Import queued: hosts.csv (csv)") for m in msgs), msgs
    done = [m for m in msgs if m.startswith("Import finished: hosts.csv (csv)")]
    assert done and "3 rows" in done[0] and "table 1" in done[0], msgs
    assert wlog.error_seq() == 0


def test_a_failing_ingest_job_is_an_error_entry(store, tmp_path):
    job = store.start_ingest_job("csv", str(tmp_path / "missing.csv"), name="missing.csv")
    store.wait_for_ingest_job(job["job_id"], timeout=30)
    errors = [e for e in wlog.entries() if e["level"] == "error"]
    assert len(errors) == 1 and errors[0]["message"].startswith("Import failed: missing.csv (csv) — ")
    assert wlog.error_seq() == errors[0]["seq"]


def test_exports_log_start_and_finish(store, client, tmp_path):
    sid = store.ingest_csv(_csv(tmp_path / "e.csv", [["a", "b"], ["c", "d"]]))["id"]
    view = store.build_view(sid, {})
    r = client.get(f"/api/export?view_id={view['view_id']}")
    assert r.status_code == 200
    msgs = [e["message"] for e in wlog.entries()]
    assert any(m.startswith(f"Export started: CSV view {view['view_id']}") for m in msgs), msgs
    fin = [m for m in msgs if m.startswith(f"Export finished: CSV view {view['view_id']}")]
    assert fin and "2 rows" in fin[0], msgs

    r = client.get("/api/export/tagged_xlsx")
    assert r.status_code == 200
    msgs = [e["message"] for e in wlog.entries()]
    assert any(m.startswith("Export finished: tagged rows from all tables (.xlsx)") and "bytes" in m for m in msgs), msgs
    assert wlog.error_seq() == 0
