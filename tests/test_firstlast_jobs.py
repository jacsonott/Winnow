"""First/Last's background jobs: what the tab runs instead of holding a
request open for a preview, Copy result or Create table.

Reported as "the First/Last page locks up the other tabs while it renders".
Measured on a 1M-row table: each preview took 15 s, the tab waited on it
with a plain POST, and an edit under Auto-update never stopped the one
before it. A few edits held every connection the browser opens to Winnow,
and a scroll in the grid took 67 s to paint. A job answers at once and is
polled; superseding one cancels it, and the cancel has to reach the SQL —
a job that only stopped being polled would still own a core for the rest
of its 15 seconds.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from winnow import store as store_module
from winnow.plugin_api import PluginRegistry

PLUGIN = "first_last"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"

ROWS = [
    ["When", "Host", "User", "EventId"],
    ["2026-03-14 08:00:00", "SRV1", "alice", "4624"],
    ["2026-03-14 09:00:00", "SRV1", "alice", "4688"],
    ["2026-03-14 17:30:00", "SRV1", "alice", "4634"],
    ["2026-03-14 10:00:00", "SRV2", "bob", "4624"],
    ["2026-03-14 11:00:00", "SRV1", "carol", "4625"],
    ["2026-03-14 12:00:00", "SRV1", "carol", "4624"],
]


@pytest.fixture
def example_registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    return reg


@pytest.fixture
def fl(client, store, write_csv, example_registry, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    rec = store.ingest_csv(write_csv(ROWS, "fl.csv"), name="fl", build_fts=False)
    spec = {"source_id": rec["id"], "group_by": ["Host", "User"], "sort_column": "When",
            "columns": ["Host", "User"]}
    return client, store, spec


_seq = iter(range(1, 1_000_000))


def _id():
    return f"fl_test{next(_seq):06d}"


def _start(client, kind, spec, jid=None, **extra):
    r = client.post(f"/api/plugin/{PLUGIN}/job/start",
                    json={"id": jid or _id(), "kind": kind, "spec": spec, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _poll(client, jid, deadline_s=20):
    end = time.monotonic() + deadline_s
    while True:
        r = client.get(f"/api/plugin/{PLUGIN}/job", params={"id": jid})
        assert r.status_code == 200, r.text
        st = r.json()
        if st["status"] != "running":
            return st
        assert time.monotonic() < end, f"job {jid} still running after {deadline_s}s"
        time.sleep(0.02)


def _route(client, route, spec):
    r = client.post(f"/api/plugin/{PLUGIN}/{route}", json=spec)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_quick_job_answers_inline_with_what_its_route_answers(fl):
    """A small table's result comes back in start's own answer, so the tab
    never polls for it — and it is the synchronous route's answer exactly:
    a job runs that code, it does not re-describe it."""
    client, _store, spec = fl
    for kind in ("preview", "rows"):
        st = _start(client, kind, spec, wait_ms=2000)
        assert st["status"] == "done", st
        assert st["result"] == _route(client, kind, spec)


def test_a_create_job_lands_the_table_and_its_timeline_tag(fl):
    client, store, spec = fl
    st = _start(client, "create", {**spec, "name": "bookends", "timeline_tag": "Sessions"}, wait_ms=2000)
    st = _poll(client, st["id"]) if st["status"] == "running" else st
    assert st["status"] == "done", st
    made = st["result"]
    assert made["source"]["name"] == "bookends" and made["source"]["row_count"] == 5
    src = store.get_source(made["source"]["id"])
    assert src["row_count"] == 5
    assert made["timeline_tag"]["name"] == "Sessions"
    counts = store.tag_counts(made["source"]["id"])["counts"]
    assert counts[str(made["timeline_tag"]["id"])] == 5


@pytest.fixture
def held_run_sql(store, monkeypatch):
    """Every run_sql a job makes waits for `release` — a job that is
    deterministically still running, without a big table."""
    release = threading.Event()
    entered = threading.Event()
    seen_tokens = []
    real = store.run_sql

    def held(sql, limit=5000, *, op_token=None):
        seen_tokens.append(op_token)
        if op_token:
            entered.set()
            assert release.wait(20), "the test never released the job"
        return real(sql, limit, op_token=op_token)

    monkeypatch.setattr(store, "run_sql", held)
    yield release, entered, seen_tokens
    release.set()


def test_a_running_job_is_polled_and_its_result_handed_over_once(fl, held_run_sql):
    client, _store, spec = fl
    release, entered, tokens = held_run_sql
    st = _start(client, "preview", spec, wait_ms=0)
    assert st["status"] == "running" and "result" not in st
    assert entered.wait(10)
    # Polling a running job is a short answer, not a wait on the work.
    t0 = time.monotonic()
    r = client.get(f"/api/plugin/{PLUGIN}/job", params={"id": st["id"]})
    assert r.json()["status"] == "running"
    assert time.monotonic() - t0 < 1.0
    release.set()
    done = _poll(client, st["id"])
    assert done["status"] == "done" and done["result"]["total_groups"] == 3
    # Every query of the job ran under its id, which is what makes a
    # cancel able to interrupt it.
    assert tokens and set(tokens) == {st["id"]}
    # Handed over once: a second ask finds nothing, rather than the
    # server holding every result it ever computed.
    again = client.get(f"/api/plugin/{PLUGIN}/job", params={"id": st["id"]})
    assert again.status_code == 400 and "No such First/Last job" in again.text


@pytest.fixture
def slow_table(client, store, write_csv, example_registry, monkeypatch):
    """A number sort column and a REGEXP that takes 5 ms a row: the
    windowed pass calls it a few times per row, so the job's query would
    run for most of a minute unless something interrupts it."""
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    rows = [["When", "Host", "Seq"]] + [
        [f"2026-03-14 08:{i // 60 % 60:02d}:{i % 60:02d}", f"H{i % 7}", str(i)] for i in range(3000)]
    rec = store.ingest_csv(write_csv(rows, "slow.csv"), name="slow", build_fts=False)
    called = threading.Event()
    real = store_module._regexp

    def slow_regexp(pattern, value):
        called.set()
        time.sleep(0.005)
        return real(pattern, value)

    monkeypatch.setattr(store_module, "_regexp", slow_regexp)
    spec = {"source_id": rec["id"], "group_by": ["Host"], "sort_column": "Seq", "columns": ["Host"]}
    return client, store, spec, called


def test_cancel_interrupts_the_query_the_job_is_running(slow_table):
    """The load-bearing one: a cancel stops the SQL itself. A job whose
    cancel only flipped a flag, or whose queries ran without the job's id
    as their op token, would run out its minute and fail the deadline."""
    client, _store, spec, called = slow_table
    st = _start(client, "preview", spec, wait_ms=0)
    assert st["status"] == "running"
    assert called.wait(10), "the job's query never started"
    t0 = time.monotonic()
    r = client.post(f"/api/plugin/{PLUGIN}/job/cancel", json={"id": st["id"]})
    assert r.status_code == 200 and r.json() == {"cancelled": True}
    end = _poll(client, st["id"], deadline_s=5)
    assert end["status"] == "cancelled", end
    assert time.monotonic() - t0 < 5


def test_a_cancel_that_arrives_before_start_still_stops_the_job(fl):
    """The id is the client's, so the tab can cancel a job whose start has
    not answered — and that cancel may reach the server first."""
    client, _store, spec = fl
    jid = _id()
    r = client.post(f"/api/plugin/{PLUGIN}/job/cancel", json={"id": jid})
    assert r.status_code == 200 and r.json() == {"cancelled": False}
    st = _start(client, "create", {**spec, "name": "never made"}, jid=jid, wait_ms=2000)
    st = _poll(client, jid) if st["status"] == "running" else st
    assert st["status"] == "cancelled", st
    assert not any(s["name"] == "never made" for s in client.get("/api/sources").json())


def test_create_stops_before_writing_when_cancelled_after_its_query(fl, monkeypatch):
    """Nothing interrupts ingest_rows, so create looks at its cancel once
    the result is computed. A cancel that lands then means no table."""
    client, store, spec = fl
    import server

    mod = server.PLUGINS.get_api(PLUGIN, "job/start")["handler"].__globals__
    real_result = mod["_result"]
    jid = _id()

    def result_then_cancel(*a, **kw):
        out = real_result(*a, **kw)
        # What job/cancel does to a job between its queries: no query is
        # running for cancel_op to interrupt, so only the flag can stop it.
        mod["_jobs"][jid]["stop"].set()
        return out

    monkeypatch.setitem(mod, "_result", result_then_cancel)
    before = {s["id"] for s in store.list_sources()}
    st = _start(client, "create", {**spec, "name": "too late"}, jid=jid, wait_ms=2000)
    st = _poll(client, jid) if st["status"] == "running" else st
    assert st["status"] == "cancelled", st
    assert {s["id"] for s in store.list_sources()} == before


@pytest.mark.parametrize("body, fragment", [
    ({"kind": "preview", "spec": {"group_by": ["Nope"], "sort_column": "When"}}, "No column 'Nope'"),
    ({"kind": "preview", "spec": {"group_by": ["Host"]}}, "orders each group"),
    ({"kind": "explode", "spec": {}}, "Unknown job kind"),
    ({"kind": "preview", "spec": "nope"}, "spec must be an object"),
    ({"kind": "preview", "spec": {}, "id": "x"}, "job id"),
    ({"kind": "preview", "spec": {}, "id": "has spaces in it"}, "job id"),
])
def test_start_refuses_a_bad_request_at_once(fl, body, fragment):
    """A grouping the route would refuse is the same 400 from start, now —
    not a job that fails a moment later."""
    client, _store, spec = fl
    if isinstance(body.get("spec"), dict):
        body = {**body, "spec": {"source_id": spec["source_id"], **body["spec"]}}
    r = client.post(f"/api/plugin/{PLUGIN}/job/start", json={"id": _id(), **body})
    assert r.status_code == 400, r.text
    assert fragment in r.json()["detail"]


def test_a_job_id_cannot_be_reused_while_it_is_held(fl, held_run_sql):
    client, _store, spec = fl
    release, entered, _tokens = held_run_sql
    jid = _id()
    _start(client, "preview", spec, jid=jid, wait_ms=0)
    assert entered.wait(10)
    r = client.post(f"/api/plugin/{PLUGIN}/job/start",
                    json={"id": jid, "kind": "preview", "spec": spec})
    assert r.status_code == 400 and "already a job" in r.text
    release.set()
    assert _poll(client, jid)["status"] == "done"


def test_a_job_of_another_case_is_gone(fl, held_run_sql, monkeypatch, tmp_path):
    """After a case switch the old case's job is not this case's to report:
    its rows describe a file that is no longer open."""
    import server

    client, _store, spec = fl
    release, entered, _tokens = held_run_sql
    st = _start(client, "preview", spec, wait_ms=0)
    assert entered.wait(10)
    other = store_module.Store(str(tmp_path / "other.db"))
    try:
        monkeypatch.setattr(server, "STORE", other)
        r = client.get(f"/api/plugin/{PLUGIN}/job", params={"id": st["id"]})
        assert r.status_code == 400 and "another case" in r.text
    finally:
        release.set()
        other.close()
