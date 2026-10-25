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


# ------------------------------------------------- deleting a case's file

def _register(client, path, name="victim"):
    r = client.post("/api/cases", json={"path": str(path), "name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_deleting_the_file_of_a_case_another_winnow_has_open_is_refused(client, tmp_path):
    """The delete route only ever checked whether THIS process had the case
    open. A second Winnow holding it (here: a Store in this test standing in
    for one — flock is per open file, so the probe sees it exactly as
    another process would) lost its case file from under it, and the
    registry entry with it."""
    import server

    other = str(tmp_path / "held.db-winnow")
    held = Store(other, default_tags=DEFAULT_TAGS)
    try:
        cid = _register(client, other)
        r = client.delete(f"/api/cases/{cid}?delete_file=true")
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["error"] == "case_in_use"
        assert "another Winnow" in detail["message"]
        assert os.path.isfile(other)
        assert any(c["id"] == cid for c in server.WS.cases.list())
    finally:
        held.close()


def test_a_heartbeat_alone_also_refuses_the_delete(client, tmp_path):
    """The share case: no flock to see, only the other machine's beat."""
    other = str(tmp_path / "shared.db-winnow")
    Store(other, default_tags=DEFAULT_TAGS).close()
    _write_marker(other, heartbeat_at=time.time() - 3)
    cid = _register(client, other)
    r = client.delete(f"/api/cases/{cid}?delete_file=true")
    assert r.status_code == 409
    assert os.path.isfile(other)


def _open_case(client, tmp_path, name):
    """A registered case this server has open, the way the home screen
    gets one: + New case creates the file, then it is opened."""
    path = str(tmp_path / f"{name}.db-winnow")
    cid = _register(client, path, name=name)
    r = client.post("/api/case/open", json={"path": path})
    assert r.status_code == 200, r.text
    return cid, path


def test_the_case_left_for_the_home_screen_can_be_deleted(client, tmp_path):
    """Going back to the case list leaves the server's case open — the home
    screen only changes what the window shows — and the delete refused a
    case this server had open ("Close this case before deleting its
    file"). Nothing on the home screen closes one, so the case the analyst
    had just come from could not be deleted until they opened another one
    and came back. The delete closes it first now."""
    import server

    cid, path = _open_case(client, tmp_path, "last")
    r = client.delete(f"/api/cases/{cid}", params={"delete_file": "true", "path": path})
    assert r.status_code == 200, r.text
    # No case is open now, and the window that asked adopts that.
    assert r.json()["case_token"] is None
    assert server.STORE is None
    assert client.get("/api/case/current").json()["open"] is False
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("last")) == []
    assert all(c["id"] != cid for c in server.WS.cases.list())


def test_deleting_another_case_leaves_the_open_one_open(client, tmp_path):
    """Only the case being deleted is closed for it: deleting a different
    one from the list must not take the open case away from a window
    still working in it."""
    import server

    _, open_path = _open_case(client, tmp_path, "working")
    other = str(tmp_path / "old.db-winnow")
    Store(other, default_tags=DEFAULT_TAGS).close()
    cid = _register(client, other, name="old")
    r = client.delete(f"/api/cases/{cid}", params={"delete_file": "true", "path": other})
    assert r.status_code == 200, r.text
    assert "case_token" not in r.json()
    assert server.STORE is not None and not server.STORE.closed
    assert server.STORE.path == open_path
    assert not os.path.exists(other)


def test_the_open_case_is_still_refused_when_another_winnow_has_it_too(client, tmp_path):
    """Closing this server's copy is not leave to delete one another Winnow
    holds: the probe still runs after the close, and still refuses."""
    import server

    cid, path = _open_case(client, tmp_path, "shared")
    # Another machine's heartbeat, written over this one's record — what a
    # second Winnow on a flock-less share does. Closing this server's Store
    # leaves a marker that names someone else where it is (release()), so
    # the probe after the close still sees them.
    _write_marker(path, heartbeat_at=time.time() - 3)
    r = client.delete(f"/api/cases/{cid}", params={"delete_file": "true", "path": path})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "case_in_use"
    assert os.path.isfile(path)
    assert any(c["id"] == cid for c in server.WS.cases.list())


def test_a_case_file_that_survives_the_delete_stays_on_the_list(client, tmp_path, monkeypatch):
    """Removing the file is best-effort (a -wal or a stale marker may well
    not be there), and on Windows a file something still has open can't be
    removed at all. The registry entry used to go regardless, so the case
    left the list while its file stayed on disk, and the analyst was told
    it was deleted."""
    import server

    cid, path = _open_case(client, tmp_path, "stuck")
    monkeypatch.setattr(server, "_remove_case_file", lambda p: None)
    r = client.delete(f"/api/cases/{cid}", params={"delete_file": "true", "path": path})
    assert r.status_code == 409
    assert "still on the case list" in r.json()["detail"]
    assert os.path.isfile(path)
    assert any(c["id"] == cid for c in server.WS.cases.list())


def test_the_case_list_says_which_case_is_open(client, tmp_path):
    """What the delete dialog reads to say the case will be closed first."""
    cid, _ = _open_case(client, tmp_path, "here")
    other = str(tmp_path / "there.db-winnow")
    Store(other, default_tags=DEFAULT_TAGS).close()
    oid = _register(client, other, name="there")
    listed = {c["id"]: c for c in client.get("/api/cases").json()}
    assert listed[cid]["open"] is True
    assert listed[oid]["open"] is False


def test_deleting_a_case_file_takes_what_lives_beside_it(client, tmp_path):
    """-wal/-shm stay behind whenever the last connection didn't close
    cleanly, and a killed Winnow leaves its stale lock marker. Deleting only
    the .db-winnow left all three in the cases folder for good."""
    other = str(tmp_path / "gone.db-winnow")
    Store(other, default_tags=DEFAULT_TAGS).close()
    for suffix in ("-wal", "-shm"):
        with open(other + suffix, "wb") as f:
            f.write(b"\0" * 32)
    _write_marker(other, heartbeat_at=time.time() - CASE_LOCK_STALE_AFTER_SEC - 60)
    cid = _register(client, other)
    r = client.delete(f"/api/cases/{cid}?delete_file=true")
    assert r.status_code == 200, r.text
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("gone")) == []


# ------------------------------------------------ one marker per case file

@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_a_case_reached_through_a_symlink_shares_its_lock(case_path, tmp_path):
    """case_lock_path used abspath, so a case opened through a link got a
    marker of its own beside the link — and a second Winnow opening the
    same file by its real path saw nothing holding it."""
    alias = str(tmp_path / "alias.db")
    os.symlink(case_path, alias)
    s = Store(alias, default_tags=DEFAULT_TAGS)
    try:
        assert case_lock_path(alias) == case_lock_path(case_path)
        holder = probe_case_lock(case_path)
        assert holder is not None and holder["pid"] == os.getpid()
    finally:
        s.close()
    assert probe_case_lock(case_path) is None


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_reopening_the_open_case_through_a_symlink_is_not_a_reopen(client, store, tmp_path):
    """Now that the marker is shared, the same-path short circuit has to
    compare the same canonical form, or this process would probe its own
    lock through the link and refuse itself."""
    import server

    alias = str(tmp_path / "alias.db")
    os.symlink(store.path, alias)
    r = client.post("/api/case/open", json={"path": alias})
    assert r.status_code == 200, r.text
    assert server.STORE is store and not store.closed


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
