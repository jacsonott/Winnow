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

from store import (CASE_LOCK_STALE_AFTER_SEC, DEFAULT_TAGS, Store, case_lock_path,
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
