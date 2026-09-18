"""The HTTP contract of a background view build: POST /api/view/start,
GET /api/view/job, POST /api/view/job/cancel, POST /api/view/adopt.

Store-level behaviour is pinned in tests/test_view_hold.py; this file
covers what the client keys on — the inline `done` for a fast search,
`running` plus a pollable id for a slow one, 404 for a superseded id, the
409 whose detail says "expired" when an adopt finds its view gone (the
client runs the search again on that word), and the idle-shutdown hold
while a job runs."""

from __future__ import annotations

import threading
import time

import pytest

from winnow.store import Store

SLOW_REGEX = "(a+)+$"


@pytest.fixture
def slow_source(store, write_csv):
    rows = [["Name", "Payload"]]
    rows += [[f"n{i}", "a" * 24 + "X"] for i in range(5000)]
    rec = store.ingest_csv(write_csv(rows), name="slow.csv", build_fts=False)
    return store, rec["id"]


def _slow_body(sid, token="tok-slow"):
    return {"source_id": sid,
            "filters": [{"column": "Payload", "op": "regex", "value": SLOW_REGEX}],
            "op_token": token}


def test_start_answers_a_fast_search_inline(client, ingested):
    store, sid = ingested
    r = client.post("/api/view/start", json={"source_id": sid, "search": "svchost"})
    assert r.status_code == 200
    job = r.json()
    assert job["status"] == "done"
    assert job["view"]["row_count"] == 1
    assert job["view"]["pending"] is True
    assert job["error"] is None
    # The held view pages like any other, before it is adopted.
    r = client.get(f"/api/rows?view_id={job['view']['view_id']}&start=0&count=5")
    assert r.status_code == 200
    assert len(r.json()["rows"]) == 1
    r = client.get(f"/api/view/job?job_id={job['job_id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "done"


def test_adopt_installs_the_held_view_with_the_view_payload(client, ingested):
    store, sid = ingested
    live = client.post("/api/view", json={"source_id": sid, "search": "svchost"}).json()
    job = client.post("/api/view/start", json={"source_id": sid, "search": "exe"}).json()
    assert job["status"] == "done"
    r = client.post("/api/view/adopt", json={"view_id": job["view"]["view_id"]})
    assert r.status_code == 200
    v = r.json()
    assert v["view_id"] == job["view"]["view_id"]
    assert v["pending"] is False
    assert set(v) >= set(live) - {"pending"}
    assert client.get(f"/api/rows?view_id={live['view_id']}&start=0&count=1").status_code == 409
    assert client.get(f"/api/rows?view_id={v['view_id']}&start=0&count=1").status_code == 200


def test_a_slow_search_polls_and_cancels(client, slow_source):
    store, sid = slow_source
    live = client.post("/api/view", json={"source_id": sid,
                                          "filters": [{"column": "Name", "op": "contains", "value": "n1"}]}).json()
    r = client.post("/api/view/start?wait_ms=50", json=_slow_body(sid))
    assert r.status_code == 200
    job = r.json()
    assert job["status"] == "running"
    assert job["view"] is None
    r = client.get(f"/api/view/job?job_id={job['job_id']}")
    assert r.status_code == 200
    assert r.json()["status"] in ("running", "cancelled")
    r = client.post(f"/api/view/job/cancel?job_id={job['job_id']}")
    assert r.status_code == 200
    assert r.json() == {"cancelled": True}
    done = store.wait_for_view_job(job["job_id"], timeout=30)
    assert done["status"] == "cancelled"
    r = client.get(f"/api/view/job?job_id={job['job_id']}")
    assert r.json()["status"] == "cancelled"
    # The rows on screen were never touched.
    assert client.get(f"/api/rows?view_id={live['view_id']}&start=0&count=1").status_code == 200
    # A miss is reported, not raised.
    assert client.post(f"/api/view/job/cancel?job_id={job['job_id']}").json() == {"cancelled": False}


def test_the_inline_wait_is_bounded(client, ingested, monkeypatch):
    """`wait_ms` parks one of the shared threadpool workers for its whole
    length, so a caller's own figure is clamped to
    Store.VIEW_JOB_INLINE_WAIT_MAX_MS — the client never sends one, and a
    stuck retry loop asking for minutes must not park every worker. The
    build waits at a gate rather than being slow (a slow regex holds the
    GIL, which would make the timing here about that, not the wait);
    unclamped, the start would answer `done` once the gate let the build
    through, not `running` within the bound."""
    store, sid = ingested
    gate = threading.Event()
    real_build = store.build_view

    def gated_build(*args, **kwargs):
        gate.wait(10)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(store, "build_view", gated_build)
    monkeypatch.setattr(Store, "VIEW_JOB_INLINE_WAIT_MAX_MS", 20)
    t0 = time.monotonic()
    job = client.post("/api/view/start?wait_ms=600000", json={"source_id": sid, "search": "svchost"}).json()
    assert job["status"] == "running"
    assert time.monotonic() - t0 < 5
    gate.set()
    assert store.wait_for_view_job(job["job_id"], timeout=10)["status"] == "done"


def test_the_chip_cancel_token_reaches_the_job(client, slow_source):
    """The client's cancel chip posts /api/cancel_op with the spec's
    op_token, same as for a blocking build — the job's build carries it."""
    store, sid = slow_source
    job = client.post("/api/view/start?wait_ms=50", json=_slow_body(sid, "tok-chip")).json()
    assert job["status"] == "running"
    r = client.post("/api/cancel_op", json={"token": "tok-chip"})
    assert r.status_code == 200
    done = store.wait_for_view_job(job["job_id"], timeout=30)
    assert done["status"] == "cancelled"


def test_polling_a_superseded_job_is_404(client, slow_source):
    store, sid = slow_source
    first = client.post("/api/view/start?wait_ms=50", json=_slow_body(sid, "tok-a")).json()
    assert first["status"] == "running"
    second = client.post("/api/view/start", json={"source_id": sid,
                                                  "filters": [{"column": "Name", "op": "contains", "value": "n2"}]}).json()
    assert second["status"] == "done"
    r = client.get(f"/api/view/job?job_id={first['job_id']}")
    assert r.status_code == 404
    assert client.get(f"/api/view/job?job_id={second['job_id']}").status_code == 200
    assert client.get("/api/view/job?job_id=999999").status_code == 404


def test_adopt_of_an_evicted_view_is_409_expired(client, ingested):
    """A normal build landed in between (a filter change): the held view
    is gone, and the detail says so in the one word the client keys on.
    Distinct from the closed-case 409, which must never say it."""
    store, sid = ingested
    job = client.post("/api/view/start", json={"source_id": sid, "search": "exe"}).json()
    assert job["status"] == "done"
    client.post("/api/view", json={"source_id": sid, "search": "cmd"})
    r = client.post("/api/view/adopt", json={"view_id": job["view"]["view_id"]})
    assert r.status_code == 409
    assert "expired" in r.json()["detail"].lower()
    r = client.post("/api/view/adopt", json={"view_id": "view_no_such"})
    assert r.status_code == 409
    assert "expired" in r.json()["detail"].lower()


def test_discard_drops_the_pending_view(client, ingested):
    store, sid = ingested
    job = client.post("/api/view/start", json={"source_id": sid, "search": "exe"}).json()
    vid = job["view"]["view_id"]
    assert client.post(f"/api/view/job/cancel?job_id={job['job_id']}").json() == {"cancelled": True}
    assert client.get(f"/api/rows?view_id={vid}&start=0&count=1").status_code == 409
    r = client.post("/api/view/adopt", json={"view_id": vid})
    assert r.status_code == 409
    assert "expired" in r.json()["detail"].lower()


def test_a_bad_filter_is_an_error_record_not_a_500(client, ingested):
    store, sid = ingested
    r = client.post("/api/view/start", json={"source_id": sid,
                                             "filter_tree": {"type": "group", "op": "AND", "children": [
                                                 {"type": "cond", "column": "Nope", "op": "contains", "value": "x"}]}})
    assert r.status_code == 200
    job = r.json()
    if job["status"] == "running":
        job = store.wait_for_view_job(job["job_id"], timeout=10)
    assert job["status"] == "error"
    assert job["error_status"] == 400
    assert "Nope" in job["error"]


def test_a_running_view_job_holds_idle_shutdown(client, slow_source):
    import server

    store, sid = slow_source
    assert server._jobs_running() is False
    job = client.post("/api/view/start?wait_ms=50", json=_slow_body(sid, "tok-idle")).json()
    assert job["status"] == "running"
    assert server._jobs_running() is True
    client.post(f"/api/view/job/cancel?job_id={job['job_id']}")
    store.wait_for_view_job(job["job_id"], timeout=30)
    assert server._jobs_running() is False


def test_a_merge_search_runs_as_a_job_too(client, store, write_csv):
    a = store.ingest_csv(write_csv([["Msg"], ["alpha beacon"], ["beta"]], "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["Msg"], ["gamma beacon"], ["delta"]], "b.csv"), name="b", build_fts=False)["id"]
    mid = store.create_merge("both", [a, b])["id"]
    live = client.post("/api/view", json={"source_id": mid}).json()
    job = client.post("/api/view/start", json={"source_id": mid, "search": "beacon"}).json()
    assert job["status"] == "done"
    assert job["view"]["row_count"] == 2
    assert client.get(f"/api/rows?view_id={live['view_id']}&start=0&count=1").status_code == 200
    r = client.post("/api/view/adopt", json={"view_id": job["view"]["view_id"]})
    assert r.status_code == 200
    rows = client.get(f"/api/rows?view_id={r.json()['view_id']}&start=0&count=5").json()["rows"]
    assert {row["source_id"] for row in rows} == {a, b}
