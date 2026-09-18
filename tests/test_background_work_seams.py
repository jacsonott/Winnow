"""Background work meeting other background work.

Three seams, all of them between features that were green apart:

* a watchlist scan walking a table that something else is still filling —
  the shell `_create_source_shell` leaves standing (`columns='[]'`) for
  the length of a save-view-as-table copy or an import;
* a derive cascade asking `start_ingest_job` for a backfill from inside a
  worker that `close()` is already draining, and the three create paths
  that ask for one with their definition rows already committed;
* the idle-shutdown predicate, which grew three more kinds of background
  work and went on telling the analyst that all four were an import.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from winnow import log as wlog
from winnow.store import DEFAULT_TAGS, OpCancelled, Store

ROWS = [["Cmd"], ["rclone copy \\\\srv\\share"], ["notepad.exe"]]

CHAIN_ROWS = [["Payload"]] + [
    [json.dumps({"user": u, "addr": a})] for u, a in [("alice", "10.0.0.5"), ("bob", "10.0.0.9")]
]


def _ingest(store, write_csv, name, rows=ROWS):
    return store.ingest_csv(write_csv(rows, name), name=name, build_fts=False)["id"]


def _view(store, sid):
    return store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})


def _hit_sources(store, wid):
    return sorted(r[0] for r in store.db.execute(
        "SELECT DISTINCT source_id FROM watchlist_hits WHERE watchlist_id=?", (wid,)))


# ------------------------------------------- a scan over a table still filling

def test_a_scan_covers_the_tables_around_one_that_is_still_filling(store, write_csv):
    """A source whose `columns` is still `'[]'` sits between two real ones
    in id order. Every indicator after it used to go unscanned: its unit
    compiled `WHERE () LIKE ?`, whose syntax error is not the "no such
    table" a unit absorbs, so it ended the whole scan."""
    store._ensure_fts_building = lambda source_id: None   # quiet, like bench's fixture
    early = _ingest(store, write_csv, "early.csv")
    shell, _table = store._create_source_shell("still filling", ["Cmd"])
    late = _ingest(store, write_csv, "late.csv")
    wid = store.add_indicator("rclone", "filename")["id"]

    assert store.scan_all()["matched"] == {wid: 2}, "one hit in each real table"
    assert _hit_sources(store, wid) == [early, late]
    # And asked for that table by name, a scan of it is simply nothing —
    # the rows aren't committed yet, and it scans when its filler finishes.
    assert store.scan_source(shell) == {"source_id": shell, "matched": {}}

    job = store.start_watchlist_scan_job()
    done = store.wait_for_watchlist_scan_job(job["job_id"], timeout=30)
    assert done["status"] == "done" and done["error"] is None
    assert done["scanned"] == done["total"] == 2, "the shell is not a table to scan"
    assert done["by_source"] == {early: 1, late: 1}


def test_a_scan_started_during_a_save_as_table_copy_finishes(case_path, write_csv, monkeypatch):
    """The same seam as the analyst meets it: a copy is mid-flight (its
    half-filled source visible to list_sources, columns still '[]') when
    the scan the watchlist tab starts walks the case."""
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    s._ensure_fts_building = lambda source_id: None
    release, t = threading.Event(), None
    try:
        sid = _ingest(s, write_csv, "events.csv")
        wid = s.add_indicator("rclone", "filename")["id"]
        v = _view(s, sid)
        copying = threading.Event()
        real_cells = Store._subset_cells

        def stalling(self, ro, cols, chunk, op_token):
            out = real_cells(self, ro, cols, chunk, op_token)
            copying.set()
            release.wait(20)
            return out
        monkeypatch.setattr(Store, "_subset_cells", stalling)
        t = threading.Thread(target=lambda: s.save_view_as_source(v["view_id"], "subset",
                                                                  build_fts=False), daemon=True)
        t.start()
        assert copying.wait(10)
        assert s.copies_in_flight() == 1

        job = s.start_watchlist_scan_job()
        done = s.wait_for_watchlist_scan_job(job["job_id"], timeout=30)
        assert done["status"] == "done" and done["error"] is None
        assert done["by_source"] == {sid: 1}
        assert _hit_sources(s, wid) == [sid]
    finally:
        release.set()
        if t is not None:
            t.join(20)
        s.close()


# --------------------------------------- a job asked for while the case closes

def test_an_ingest_job_asked_for_while_the_case_closes_is_refused(case_path, write_csv):
    """The other side of close()'s flag-and-snapshot step, for the job
    registry: a job registered after the snapshot would run its batches
    into the connection close() is about to shut. Deterministic — the
    flag is set the way close() sets it, not raced for."""
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    try:
        path = write_csv(ROWS, "late.csv")
        with s._ingest_jobs_lock:
            s._closing = True
        with pytest.raises(OpCancelled):
            s.start_ingest_job("csv", path, name="late.csv")
        assert s.list_ingest_jobs() == [], "nothing registered, so nothing to drain"
    finally:
        s._closing = False
        s.close()


def test_the_derive_create_paths_leave_no_definition_behind_when_the_case_closes(case_path, write_csv):
    """All three commit their definition row and THEN ask for the backfill
    job, so a refusal has to take the definition with it. A column left
    'building' with no job in existence never becomes anything else: the
    header reads "(building…)" after every reopen, save_view_as_source
    refuses the whole table while it stands, and nothing can chain off it.
    Deterministic — the flag is set the way close() sets it."""
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    sid = None
    try:
        sid = s.ingest_csv(write_csv(CHAIN_ROWS, "c.csv"), name="c", build_fts=False)["id"]
        made = s.add_derived_column(sid, "Addr", "Payload", "json_field", {"path": "$.addr"})
        s.wait_for_ingest_job(made["job_id"], timeout=30)
        before = s.get_derived_column(made["definition"]["id"])
        assert before["status"] == "ready"

        with s._ingest_jobs_lock:
            s._closing = True

        with pytest.raises(OpCancelled):
            s.add_derived_column(sid, "User", "Payload", "json_field", {"path": "$.user"})
        with pytest.raises(OpCancelled):
            s.add_derived_columns(sid, [
                {"name": "U1", "input_column": "Payload", "op_id": "json_field",
                 "params": {"path": "$.user"}},
                {"name": "U2", "input_column": "Payload", "op_id": "json_field",
                 "params": {"path": "$.addr"}},
            ])
        with pytest.raises(OpCancelled):
            s.rederive_column(before["id"], {"path": "$.user"})

        assert [d["name"] for d in s.list_derived_columns(sid)] == ["Addr"], \
            "a create whose backfill never started leaves no column"
        assert s.get_derived_column(before["id"]) == before, \
            "a re-derive that never ran changes nothing about the definition"
        assert [j["job_id"] for j in s.list_ingest_jobs()] == [made["job_id"]]
    finally:
        s._closing = False
        s.close()
    # And the case says the same thing when it is opened again — which is
    # where a stuck 'building' would have shown up.
    s2 = Store(case_path, default_tags=DEFAULT_TAGS)
    try:
        assert [(c["name"], c["derived_status"]) for c in s2.get_source(sid)["columns"]
                if c.get("derived")] == [("Addr", "ready")]
    finally:
        s2.close()


def test_a_derive_cascade_mid_close_starts_no_backfill_and_marks_the_child_partial(
        case_path, write_csv, monkeypatch):
    """A re-derive cascades to its children by starting another job — from
    a worker close() is draining, so the child job would land after the
    snapshot and back-fill into a closing connection. The cascade treats
    the refusal like any other failure, which means recording it: the
    child is marked 'partial' (its values were computed from the parent's
    data as it stood BEFORE the re-derive, and 'ready' would call that
    finished) with a line in the log naming it, rather than left stuck
    'building'. The parent job keeps its 'done'."""
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    mark = wlog.seq()
    closed = False
    try:
        sid = s.ingest_csv(write_csv(CHAIN_ROWS, "c.csv"), name="c", build_fts=False)["id"]
        parent = s.add_derived_column(sid, "Addr", "Payload", "json_field", {"path": "$.addr"})
        s.wait_for_ingest_job(parent["job_id"], timeout=30)
        child = s.add_derived_column(sid, "Net", "Addr", "regex_extract",
                                     {"pattern": r"^(\d+\.\d+\.\d+)\."})
        s.wait_for_ingest_job(child["job_id"], timeout=30)

        at_cascade = threading.Event()
        real_dependents = Store.dependent_derived_columns

        def gated(self, source_id, names):
            # Only the worker's call waits — rederive_column makes the same
            # call on this thread to report what it cascades to.
            if threading.current_thread() is not threading.main_thread():
                at_cascade.set()
                deadline = time.time() + 20
                while not self._closing and time.time() < deadline:
                    time.sleep(0.01)
            return real_dependents(self, source_id, names)
        monkeypatch.setattr(Store, "dependent_derived_columns", gated)

        res = s.rederive_column(parent["definition"]["id"], {"path": "$.user"})
        assert res["cascades_to"] == ["Net"]
        assert at_cascade.wait(30), "the worker never reached its cascade"
        t0 = time.time()
        s.close()   # must drain the worker and return, not hang or crash
        closed = True
        assert time.time() - t0 < 30

        jobs = s.list_ingest_jobs()
        assert not [j for j in jobs if "recomputed" in j["name"]], jobs
        assert [j["status"] for j in jobs if j["job_id"] == res["job_id"]] == ["done"]
    finally:
        if not closed:
            s.close()
    con = sqlite3.connect(case_path)
    try:
        assert dict(con.execute("SELECT name, status FROM derived_columns")) == {
            "Addr": "ready", "Net": "partial"}, \
            "'ready' calls pre-re-derive values finished; 'building' blocks the chain below it"
    finally:
        con.close()
    # Net's own create and its first backfill log too; the one this is
    # about is the warning, and it has to name the column and say what to
    # do about it — the log is where a reopened case explains itself.
    said = [e for e in wlog.entries()
            if e["seq"] > mark and e["level"] == "warn" and "Net" in e["message"]]
    assert len(said) == 1, [e["message"] for e in wlog.entries() if e["seq"] > mark]
    assert "re-derive" in said[0]["message"], said[0]


# ------------------------------------------ what the 409 says is holding it up

def test_copy_sources_names_the_scan_it_is_waiting_for(client, store, write_csv, tmp_path):
    """All four kinds of background work block this route, and for the
    same reason — but three of them are not imports, and an analyst told
    "Still importing" goes looking at a jobs panel with no import in it."""
    import server

    store._ensure_fts_building = lambda source_id: None
    sid = _ingest(store, write_csv, "s.csv")
    store.add_indicator("rclone", "filename")
    target = str(tmp_path / "real-case.db")
    Store(target).close()
    gate = threading.Event()
    entered = threading.Event()
    real_unit = store._scan_unit

    def gated_unit(src, cols, ind):
        entered.set()
        gate.wait(20)
        return real_unit(src, cols, ind)

    store._scan_unit = gated_unit
    try:
        job = store.start_watchlist_scan_job()
        assert entered.wait(10)
        assert server._busy_reason() == "A watchlist scan is still running"
        r = client.post("/api/case/copy_sources",
                        json={"target_path": target, "source_ids": [sid]})
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert "watchlist scan" in detail and "importing" not in detail.lower(), detail
    finally:
        gate.set()
        del store._scan_unit
        store.wait_for_watchlist_scan_job(job["job_id"], timeout=30)


def test_a_copy_in_flight_reads_as_a_copy_not_an_import(case_path, write_csv, monkeypatch):
    """Same wording question for the other quiet one: the source a
    save-as-table copy is filling has no ingest job behind it."""
    import server

    s = Store(case_path, default_tags=DEFAULT_TAGS)
    monkeypatch.setattr(server, "STORE", s)
    release, t = threading.Event(), None
    try:
        sid = _ingest(s, write_csv, "events.csv")
        v = _view(s, sid)
        copying = threading.Event()
        real_cells = Store._subset_cells

        def stalling(self, ro, cols, chunk, op_token):
            out = real_cells(self, ro, cols, chunk, op_token)
            copying.set()
            release.wait(20)
            return out
        monkeypatch.setattr(Store, "_subset_cells", stalling)
        t = threading.Thread(target=lambda: s.save_view_as_source(v["view_id"], "subset",
                                                                  build_fts=False), daemon=True)
        t.start()
        assert copying.wait(10)
        assert server._busy_reason() == "A table is still being saved"
    finally:
        release.set()
        if t is not None:
            t.join(20)
        s.close()
    assert server._busy_reason() is None, "a closed store is not busy"
