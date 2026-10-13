"""The advisory "this case is open in another Winnow" lock.

Two independent signals decide whether a case is held (see
store.probe_case_lock): the flock, which is exact but only on a local
filesystem, and the heartbeat in the marker's contents, which is the half
that still works on the network share where two analysts actually collide.
Each is exercised on its own here, because on the developer's local disk the
flock alone would pass every test and hide a broken heartbeat.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from winnow.store import (CASE_LOCK_STALE_AFTER_SEC, DEFAULT_TAGS, Store, case_lock_path,
                   describe_case_lock, probe_case_lock)


def _write_marker(case_path, **fields):
    """A marker with no flock behind it — what a Winnow on a filesystem
    without working flock leaves, and what a killed one leaves behind."""
    rec = {"host": "other-box", "user": "analyst2", "pid": 999999,
           "case_path": case_path, "started_at": "2026-08-19T09:14:00",
           "heartbeat_at": time.time()}
    rec.update(fields)
    with open(case_lock_path(case_path), "w", encoding="utf-8") as f:
        json.dump(rec, f)


def test_unopened_case_is_free(case_path):
    assert probe_case_lock(case_path) is None


def test_live_store_is_reported_held_and_names_itself(case_path):
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    try:
        holder = probe_case_lock(case_path)
        assert holder is not None
        assert holder["pid"] == os.getpid()
        assert holder["evidence"] == "flock"
        assert str(os.getpid()) in describe_case_lock(holder)
    finally:
        s.close()


def test_close_releases_the_lock_and_removes_the_marker(case_path):
    Store(case_path, default_tags=DEFAULT_TAGS).close()
    assert probe_case_lock(case_path) is None
    assert not os.path.exists(case_lock_path(case_path))


def test_fresh_heartbeat_holds_it_even_with_no_flock(case_path):
    """The share case. Nothing holds an flock on this marker — if the
    heartbeat weren't consulted the probe would call it free, which is
    exactly the collision the lock exists to catch."""
    _write_marker(case_path, heartbeat_at=time.time() - 5)
    holder = probe_case_lock(case_path)
    assert holder is not None
    assert holder["evidence"] == "heartbeat"
    assert holder["user"] == "analyst2"
    assert holder["heartbeat_age_sec"] == pytest.approx(5, abs=1)


def test_stale_marker_from_a_killed_process_reads_as_free(case_path):
    _write_marker(case_path, heartbeat_at=time.time() - CASE_LOCK_STALE_AFTER_SEC - 60)
    assert probe_case_lock(case_path) is None


def test_a_stale_marker_does_not_stop_a_new_store(case_path):
    _write_marker(case_path, heartbeat_at=time.time() - CASE_LOCK_STALE_AFTER_SEC - 60)
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    try:
        holder = probe_case_lock(case_path)
        assert holder["pid"] == os.getpid()  # overwritten in place, not appended to
    finally:
        s.close()


def test_unparseable_marker_falls_back_to_the_flock(case_path):
    """A torn read is expected — the marker is rewritten in place every
    heartbeat — so a corrupt record must not be mistaken for "free" while a
    live Store still holds the flock."""
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    try:
        with open(case_lock_path(case_path), "w", encoding="utf-8") as f:
            f.write('{"host": "trunc')
        holder = probe_case_lock(case_path)
        assert holder is not None and holder["evidence"] == "flock"
    finally:
        s.close()


def test_release_leaves_a_marker_that_names_someone_else(case_path):
    """"Open anyway" on a filesystem without flock means the second Winnow
    overwrote the record. The first one closing must not then delete a
    marker describing a process that is still live."""
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    _write_marker(case_path)  # a "second Winnow" claims it
    s.close()
    assert os.path.exists(case_lock_path(case_path))
    assert probe_case_lock(case_path)["user"] == "analyst2"


# ------------------------------------------------------------------ HTTP layer

def test_open_conflicting_case_409s_with_the_holder(client, tmp_path, monkeypatch):
    import server

    other = str(tmp_path / "other.db")
    Store(other, default_tags=DEFAULT_TAGS).close()
    _write_marker(other, heartbeat_at=time.time() - 3)

    r = client.post("/api/case/open", json={"path": other})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "case_in_use"
    assert detail["holder"]["user"] == "analyst2"
    assert "analyst2" in detail["message"]


def test_force_opens_it_anyway(client, tmp_path):
    import server

    other = str(tmp_path / "other.db")
    Store(other, default_tags=DEFAULT_TAGS).close()
    _write_marker(other, heartbeat_at=time.time() - 3)

    r = client.post("/api/case/open", json={"path": other, "force": True})
    assert r.status_code == 200
    # The client fixture closes whatever Store this route left behind.
    assert os.path.abspath(server.STORE.path) == os.path.abspath(other)


def test_reopening_the_open_case_is_not_a_self_conflict(client, store):
    """api_case_open opens the new Store before closing the old one, so
    without the same-path short circuit this process would probe its own
    lock and refuse to reopen the case it already has."""
    r = client.post("/api/case/open", json={"path": store.path})
    assert r.status_code == 200
    assert r.json()["sources"] == store.list_sources()


def test_reopening_a_closed_store_at_the_same_path_really_reopens(client, store, case_path):
    """The same-path short circuit must not swallow a genuine reopen.
    server.STORE outlives Store.close() on both the case-switch path and the
    legacy-preset migration, so "same path" alone is not "already open" —
    short-circuiting there returns rows from a closed connection, which
    surfaces as a 409 from the closed-database handler."""
    import server

    store.close()
    r = client.post("/api/case/open", json={"path": case_path})
    assert r.status_code == 200
    assert server.STORE is not store and not server.STORE.closed


# ------------------------------------------------------ concurrent switches

def _race_two_opens(client, monkeypatch, paths):
    """POST /api/case/open for each path from its own thread, arranged so
    that both requests are inside _open_store at the same moment if
    nothing serialises them. Returns (statuses, every Store opened).

    The barrier is what makes this deterministic rather than a race the
    test usually wins: unserialised, both requests read `old = STORE` and
    then meet here, so neither can swap STORE before the other has
    committed to its own open. Serialised, the second request never
    arrives while the first waits; the wait times out, the barrier
    breaks, and each request runs alone — one second, not a hang."""
    import threading

    import server

    meet = threading.Barrier(len(paths))
    made = []
    real = server._open_store

    def open_store(path):
        try:
            meet.wait(timeout=1.0)
        except threading.BrokenBarrierError:
            pass
        s = real(path)
        made.append(s)
        return s

    monkeypatch.setattr(server, "_open_store", open_store)
    statuses = {}

    def go(i, p):
        statuses[i] = client.post("/api/case/open", json={"path": p}).status_code

    threads = [threading.Thread(target=go, args=(i, p)) for i, p in enumerate(paths)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return [statuses.get(i) for i in range(len(paths))], made


def test_two_case_opens_at_once_leave_no_store_open_behind(client, tmp_path, monkeypatch):
    """Double-clicking a case card (or two windows switching at once) sent
    two /api/case/open whose `old = STORE; STORE = new; old.close()` steps
    interleaved: one freshly opened Store was never closed. Its flock and
    heartbeat stayed held for the life of the process, so that case then
    read as "already open in another Winnow" — naming this very server."""
    import server

    a, b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    for p in (a, b):
        Store(p, default_tags=DEFAULT_TAGS).close()
    statuses, made = _race_two_opens(client, monkeypatch, [a, b])
    leaked = [s for s in made if s is not server.STORE and not s.closed]
    try:
        assert statuses == [200, 200]
        assert not leaked, f"left open behind the switch: {[s.path for s in leaked]}"
        # The case that lost the race is free again, not held by a ghost.
        loser = b if os.path.abspath(server.STORE.path) == os.path.abspath(a) else a
        assert probe_case_lock(loser) is None
    finally:
        for s in leaked:
            s.close()


def test_two_opens_of_the_same_case_make_one_store(client, tmp_path, monkeypatch):
    """The same double-click on one card: both requests passed the
    already-open check before either had opened anything, so the case got
    TWO writer Stores in one process — and the one not kept stayed open."""
    import server

    a = str(tmp_path / "a.db")
    Store(a, default_tags=DEFAULT_TAGS).close()
    statuses, made = _race_two_opens(client, monkeypatch, [a, a])
    leaked = [s for s in made if s is not server.STORE and not s.closed]
    try:
        assert statuses == [200, 200]
        assert len(made) == 1, "a second writer Store was opened on the same case file"
        assert not leaked
    finally:
        for s in leaked:
            s.close()
