"""The watchlist scan: off the writer lock, complete, and a background job.

Every (indicator, source) unit matches on a reader connection and takes
Store.lock only for the one transaction that writes its hits and
auto-tags — never for the match, which is a full LIKE scan on a table
with no index yet and, held under the lock (where it ran, once per
indicator per table, inside one POST), stalled every locked read for
the length of the scan. The lock discipline is asserted structurally
(the _CountingLock pattern from test_search.py, plus a spy on which
connection ran which statement) rather than by racing a thread, which
passes against the broken shape too.

The job layer is the same scan on a thread (_iter_watchlist_scan behind
scan_source/scan_all and the worker), on _JobRegistry: one live scan,
superseded ids unpollable, cooperative cancel between units, drained by
Store.close().
"""

from __future__ import annotations

import contextlib
import threading
import time

import pytest

from winnow.store import DEFAULT_TAGS, Store


class _CountingLock:
    """Delegates to a real lock while counting how many times it goes fully
    unheld — i.e. how many separate units of work an operation splits into
    (see test_search.py for the reasoning)."""

    def __init__(self, inner):
        self._inner = inner
        self.depth = 0
        self.released_to_zero = 0

    def acquire(self, *a, **kw):
        got = self._inner.acquire(*a, **kw)
        if got:
            self.depth += 1
        return got

    def release(self):
        self._inner.release()
        self.depth -= 1
        if self.depth == 0:
            self.released_to_zero += 1

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def _settled_unheld(counting, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if counting.depth == 0:
            return True
        time.sleep(0.02)
    return counting.depth == 0


class _SqlSpy:
    """A connection stand-in recording every statement it runs, tagged
    with which side (writer/reader) ran it. Everything else passes
    through, including the transaction context manager."""

    def __init__(self, inner, log, who):
        self._inner, self._log, self._who = inner, log, who

    def execute(self, sql, *a, **kw):
        self._log.append((self._who, sql))
        return self._inner.execute(sql, *a, **kw)

    def executemany(self, sql, *a, **kw):
        self._log.append((self._who, sql))
        return self._inner.executemany(sql, *a, **kw)

    def __enter__(self):
        return self._inner.__enter__()

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _is_match(sql):
    return ("LIKE" in sql or "instr(" in sql) and "watchlist" not in sql


def _hit_rids(store, wid, sid):
    return [r[0] for r in store.db.execute(
        "SELECT rid FROM watchlist_hits WHERE watchlist_id=? AND source_id=? ORDER BY rid", (wid, sid))]


def _ingest(store, write_csv, name, rows, **kw):
    kw.setdefault("build_fts", False)
    return store.ingest_csv(write_csv(rows, name=name), name=name, **kw)["id"]


# ------------------------------------------------------------ lock discipline

def test_matches_run_on_readers_with_the_lock_unheld_and_each_unit_writes_once(store, write_csv):
    """Invariant #4, structurally: every match statement runs on a reader
    connection checked out while the writer lock is unheld, none on the
    writer; and the lock goes fully unheld at least once per (indicator,
    source) unit — one committed write each, never a loop-wide hold."""
    sids = [_ingest(store, write_csv, f"lk{i}.csv", [["Process"]] + [["svchost.exe"]] * 100) for i in range(3)]
    wa = store.add_indicator("svchost", "filename")["id"]
    wb = store.add_indicator("lsass", "filename")["id"]

    log: list[tuple[str, str]] = []
    checkouts: list[int] = []
    counting = _CountingLock(store.lock)
    real_reader = store._reader

    @contextlib.contextmanager
    def spy_reader():
        checkouts.append(counting.depth)
        with real_reader() as ro:
            yield _SqlSpy(ro, log, "reader")

    real_db = store.db
    store.lock = counting
    store._reader = spy_reader
    store.db = _SqlSpy(real_db, log, "writer")
    # The scan fires the background index build for an unindexed table
    # (fire-and-forget, its own tests); that thread's chunked lock holds
    # would be counted here too — the depth a checkout sees must be this
    # thread's own. Quieted the way bench/'s fixture quiets it.
    store._ensure_fts_building = lambda source_id: None
    try:
        res = store.scan_all()
    finally:
        store.lock = counting._inner
        store.db = real_db
        del store._reader
        del store._ensure_fts_building
    assert res["matched"] == {wa: 300, wb: 0}
    matches = [(who, sql) for who, sql in log if _is_match(sql)]
    assert len(matches) == 6, matches
    assert all(who == "reader" for who, _ in matches), matches
    assert not any(who == "writer" and _is_match(sql) for who, sql in log)
    assert checkouts and all(d == 0 for d in checkouts), checkouts
    assert _settled_unheld(counting)
    assert counting.released_to_zero >= 6
    for sid in sids:
        assert _hit_rids(store, wa, sid) == list(range(1, 101))
        assert _hit_rids(store, wb, sid) == []


def test_list_indicators_reads_while_the_writer_lock_is_held(store):
    """One LEFT JOIN … GROUP BY on the reader pool: the list behind
    /api/watchlist and the badge poll does not queue behind a build."""
    store.add_indicator("evil.exe", "filename")
    out = []
    with store.lock:
        t = threading.Thread(target=lambda: out.append(store.list_indicators()))
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), "list_indicators waited on the writer lock"
    assert [i["value"] for i in out[0]] == ["evil.exe"]
    assert out[0][0]["hit_count"] == 0


# ----------------------------------------------------------- what it matches

def test_hits_are_identical_with_and_without_the_index_and_the_index_is_used(store, write_csv):
    rows = [["Path"],
            ["C:\\users\\jacso\\desktop\\file.txt"],
            ["C:\\Windows\\System32\\cmd.exe"],
            ["notes about JACSO here"],
            ["nothing"]]
    a = _ingest(store, write_csv, "a.csv", rows)
    b = _ingest(store, write_csv, "b.csv", rows, build_fts=True)
    assert store.wait_for_fts(b, timeout=10)
    assert not store.get_source(a)["has_fts"]
    wid = store.add_indicator("jacso", "other")["id"]

    log: list[tuple[str, str]] = []
    real_reader = store._reader

    @contextlib.contextmanager
    def spy_reader():
        with real_reader() as ro:
            yield _SqlSpy(ro, log, "reader")

    store._reader = spy_reader
    try:
        assert store.scan_source(a)["matched"] == {wid: 2}
        sql_a = [sql for _, sql in log if _is_match(sql)]
        del log[:]
        assert store.scan_source(b)["matched"] == {wid: 2}
        sql_b = [sql for _, sql in log if _is_match(sql)]
    finally:
        del store._reader
    # Same rows either way (case-insensitive, mid-token) …
    assert _hit_rids(store, wid, a) == _hit_rids(store, wid, b) == [1, 3]
    # … and the indexed table really took the trigram path, the other the blob LIKE.
    assert sql_a and not any("doc LIKE" in s for s in sql_a) and all("ESCAPE" in s for s in sql_a)
    assert sql_b and all("doc LIKE" in s for s in sql_b) and not any("ESCAPE" in s for s in sql_b)


def test_wildcard_characters_in_an_indicator_stay_literal_with_the_index_built(store, write_csv):
    """`%`/`_` cannot take the bare (unescaped) trigram pushdown — they
    would match as wildcards, a silently-wrong superset — so those values
    route through the escaped fallback even with the index ready."""
    rows = [["Note"], ["progress 100% done"], ["progress 100X done"], ["under_score"], ["underXscore"]]
    sid = _ingest(store, write_csv, "wild.csv", rows, build_fts=True)
    assert store.wait_for_fts(sid, timeout=10)
    w1 = store.add_indicator("100%", "other")["id"]
    w2 = store.add_indicator("under_score", "other")["id"]
    assert store.scan_source(sid)["matched"] == {w1: 1, w2: 1}
    assert _hit_rids(store, w1, sid) == [1]
    assert _hit_rids(store, w2, sid) == [3]


def test_a_scan_scoped_to_one_indicator_leaves_the_others_hits_alone(client, store, write_csv):
    sid = _ingest(store, write_csv, "s.csv", [["A"], ["alpha"], ["beta"], ["alpha beta"]])
    wa = store.add_indicator("alpha", "other")["id"]
    wb = store.add_indicator("beta", "other")["id"]
    assert store.scan_source(sid)["matched"] == {wa: 2, wb: 2}
    store.db.execute("DELETE FROM watchlist_hits WHERE watchlist_id=?", (wb,))
    store.db.commit()
    assert store.scan_source(sid, watchlist_ids=[wa])["matched"] == {wa: 2}
    assert _hit_rids(store, wb, sid) == []            # untouched by a scan for alpha
    assert _hit_rids(store, wa, sid) == [1, 3]
    r = client.post(f"/api/watchlist/scan?source_id={sid}&watchlist_id={wb}")
    assert r.status_code == 200 and r.json()["matched"] == {str(wb): 2}
    assert _hit_rids(store, wb, sid) == [2, 3]
    assert client.post(f"/api/watchlist/scan?watchlist_id={wa}").json() == {"matched": {str(wa): 2}}


def test_an_indicator_deleted_while_its_match_runs_writes_nothing_even_when_its_id_is_reused(store, write_csv):
    """The write re-reads the indicator under the lock. watchlist.id is not
    AUTOINCREMENT: removing the newest entry and adding another reuses its
    id, and a scan that read rids for the old value before the delete
    would otherwise file them — and its auto-tag — under the new one."""
    sid = _ingest(store, write_csv, "d.csv", [["Cmd"], ["rclone copy"], ["rclone sync"], ["notepad"]])
    tag = store.list_tags()[0]["id"]
    wid = store.add_indicator("rclone", "filename", None, tag)["id"]
    real_reader = store._reader
    fired = []

    @contextlib.contextmanager
    def reader_then_delete():
        log: list[tuple[str, str]] = []
        with real_reader() as ro:
            yield _SqlSpy(ro, log, "reader")
        # After the match's SELECT, before the write: the analyst removes
        # the entry and adds another, which takes the same id.
        if not fired and any(_is_match(sql) for _, sql in log):
            fired.append(1)
            store.delete_indicator(wid)
            assert store.add_indicator("mimikatz", "filename")["id"] == wid

    store._reader = reader_then_delete
    try:
        res = store.scan_source(sid)
    finally:
        del store._reader
    assert fired
    assert res["matched"] == {}                                   # the unit wrote nothing
    assert _hit_rids(store, wid, sid) == []
    assert store.list_indicators() == [{"id": wid, "value": "mimikatz", "kind": "filename", "note": None,
                                        "auto_tag_id": None, "hit_count": 0}]
    assert store.db.execute("SELECT COUNT(*) FROM row_tags WHERE source_id=?", (sid,)).fetchone()[0] == 0
    assert store.scan_source(sid)["matched"] == {wid: 0}          # a plain scan for the new value


def test_auto_tags_land_in_the_same_transaction_as_the_hits(store, write_csv):
    sid = _ingest(store, write_csv, "t.csv", [["Cmd"], ["rclone copy"], ["notepad"], ["RCLONE.exe"]])
    tag = store.list_tags()[0]["id"]
    wid = store.add_indicator("rclone", "filename", None, tag)["id"]
    assert store.scan_source(sid)["matched"] == {wid: 2}
    tagged = [r[0] for r in store.db.execute(
        "SELECT rid FROM row_tags WHERE source_id=? AND tag_id=? ORDER BY rid", (sid, tag))]
    assert tagged == [1, 3] == _hit_rids(store, wid, sid)
    # Idempotent: a second scan replaces the slice and moves no tags.
    assert store.scan_source(sid)["matched"] == {wid: 2}
    assert store.db.execute("SELECT COUNT(*) FROM row_tags WHERE source_id=?", (sid,)).fetchone()[0] == 2


def test_an_exact_duplicate_value_is_refused(client, store):
    store.add_indicator("evil.exe", "filename")
    with pytest.raises(ValueError, match="already on the watchlist"):
        store.add_indicator("evil.exe", "filename")
    r = client.post("/api/watchlist", json={"value": "evil.exe", "kind": "other"})
    assert r.status_code == 400
    assert "already" in r.json()["detail"]
    # Exact means exact: a different case is a different indicator.
    assert client.post("/api/watchlist", json={"value": "EVIL.EXE", "kind": "other"}).status_code == 200
    # The imports keep skipping duplicates silently, and name what they added.
    imp = client.post("/api/watchlist/import", json={"text": "evil.exe\nnew.exe\n"}).json()
    assert imp["added"] == 1
    new_id = next(i["id"] for i in imp["indicators"] if i["value"] == "new.exe")
    assert imp["added_ids"] == [new_id]
    assert len(store.list_indicators()) == 3


def test_a_merge_in_the_case_is_skipped_without_error(store, write_csv):
    a = _ingest(store, write_csv, "a.csv", [["Process"], ["svchost.exe"], ["lsass.exe"]])
    b = _ingest(store, write_csv, "b.csv", [["Process"], ["svchost.exe"], ["svchost.exe"]])
    merge = store.create_merge("m", [a, b])
    assert merge["id"] < 0
    wid = store.add_indicator("svchost", "filename")["id"]
    assert store.scan_all()["matched"] == {wid: 3}
    assert store.scan_source(merge["id"]) == {"source_id": merge["id"], "matched": {}}
    assert {s["source_id"] for s in store.indicator_hits(wid)["sources"]} == {a, b}
    job = store.start_watchlist_scan_job()
    done = store.wait_for_watchlist_scan_job(job["job_id"], timeout=20)
    assert done["status"] == "done" and done["error"] is None
    assert done["total"] == done["scanned"] == 2          # the merge is not a table to scan
    assert done["matched"] == {wid: 3}


# ------------------------------------------------------------------ the job

def test_the_job_runs_the_same_scan_and_reports_progress_and_totals(store, write_csv):
    sids = [_ingest(store, write_csv, f"j{i}.csv", [["Process"]] + [["svchost.exe"]] * (i + 1) + [["lsass.exe"]])
            for i in range(3)]
    tag = store.list_tags()[0]["id"]
    ws = store.add_indicator("svchost", "filename")["id"]
    wl = store.add_indicator("lsass", "filename", None, tag)["id"]
    expected = store.scan_all()["matched"]
    assert expected == {ws: 6, wl: 3}

    job = store.start_watchlist_scan_job()
    assert job["status"] in ("running", "done") and job["job_id"]
    done = store.wait_for_watchlist_scan_job(job["job_id"], timeout=20)
    assert done["status"] == "done" and done["error"] is None
    assert done["matched"] == expected
    assert done["scanned"] == done["total"] == 3
    assert done["by_source"] == {sids[0]: 2, sids[1]: 3, sids[2]: 4}
    assert done["auto_tagged"] == sids                    # lsass carries a tag, and hit every table
    assert done["elapsed_ms"] is not None
    assert store.get_watchlist_scan_job(job["job_id"])["status"] == "done"
    assert store.get_watchlist_scan_job(job["job_id"] + 99) is None
    assert store.running_watchlist_scan_jobs() == 0
    # Scoped both ways.
    job = store.start_watchlist_scan_job(source_ids=[sids[1]], watchlist_ids=[ws])
    done = store.wait_for_watchlist_scan_job(job["job_id"], timeout=20)
    assert done["total"] == 1 and done["matched"] == {ws: 2} and done["auto_tagged"] == []


def test_a_second_scan_supersedes_the_first_and_cancel_stops_between_units(store, write_csv):
    for i in range(2):
        _ingest(store, write_csv, f"c{i}.csv", [["Process"], ["svchost.exe"]])
    store.add_indicator("svchost", "filename")
    store.add_indicator("lsass", "filename")
    gate = threading.Event()
    entered = threading.Event()
    units: list[tuple[int, int, threading.Thread]] = []
    real_unit = store._scan_unit

    def gated_unit(src, cols, ind):
        units.append((src["id"], ind["id"], threading.current_thread()))
        entered.set()
        gate.wait(10)
        return real_unit(src, cols, ind)

    store._scan_unit = gated_unit
    try:
        first = store.start_watchlist_scan_job()
        assert entered.wait(5)
        assert first["status"] == "running"
        assert store.get_watchlist_scan_job(first["job_id"])["status"] == "running"
        assert store.running_watchlist_scan_jobs() == 1
        # A newer scan replaces it: the old id is unpollable at once.
        second = store.start_watchlist_scan_job()
        assert second["job_id"] != first["job_id"]
        assert store.get_watchlist_scan_job(first["job_id"]) is None
        assert store.cancel_watchlist_scan_job(first["job_id"]) is False
        gate.set()
        done = store.wait_for_watchlist_scan_job(second["job_id"], timeout=20)
        assert done["status"] == "done"
        for _, _, t in units:
            t.join(10)
        # The first stopped after the unit it was in; the second ran all four.
        first_units = [u for u in units if u[2] is units[0][2]]
        assert len(first_units) == 1
        assert len(units) == 1 + 4

        # Cancel: asked at its next unit, the scan ends "cancelled".
        del units[:]
        gate.clear()
        entered.clear()
        job = store.start_watchlist_scan_job()
        assert entered.wait(5)
        assert store.cancel_watchlist_scan_job(job["job_id"]) is True
        gate.set()
        done = store.wait_for_watchlist_scan_job(job["job_id"], timeout=20)
        assert done["status"] == "cancelled"
        assert len(units) == 1
        assert store.cancel_watchlist_scan_job(job["job_id"]) is False    # finished: a miss, not an error
        assert store.cancel_watchlist_scan_job(999999) is False
    finally:
        gate.set()
        del store._scan_unit


def test_close_with_a_running_scan_stops_it_and_returns(case_path, write_csv):
    """close() flags the live scan, joins its thread and only then closes
    the connection; a unit in flight finishes its write on the open
    connection and the worker stops at the next unit."""
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    for i in range(2):
        s.ingest_csv(write_csv([["Process"], ["svchost.exe"]], name=f"z{i}.csv"), name=f"z{i}.csv", build_fts=False)
    s.add_indicator("svchost", "filename")
    entered = threading.Event()
    real_unit = s._scan_unit

    def unit_that_waits_for_close(src, cols, ind):
        entered.set()
        deadline = time.time() + 10
        while not s._scan_jobs.closing and time.time() < deadline:
            time.sleep(0.01)
        return real_unit(src, cols, ind)

    s._scan_unit = unit_that_waits_for_close
    job = s.start_watchlist_scan_job()
    assert entered.wait(5)
    t0 = time.time()
    s.close()   # must stop the scan and return, not hang or crash
    assert time.time() - t0 < 30
    assert s.closed is True
    assert s.get_watchlist_scan_job(job["job_id"])["status"] == "cancelled"
    assert s.running_watchlist_scan_jobs() == 0


# --------------------------------------------------------------- the routes

def test_scan_job_routes(client, store, write_csv):
    sid = _ingest(store, write_csv, "r.csv", [["Cmd"], ["rclone copy"], ["notepad"]])
    wid = client.post("/api/watchlist", json={"value": "rclone", "kind": "filename"}).json()["id"]
    r = client.post("/api/watchlist/scan/start", json={"watchlist_ids": [wid]})
    assert r.status_code == 200
    job = r.json()
    assert job["status"] in ("running", "done")
    assert store.wait_for_watchlist_scan_job(job["job_id"], timeout=20)["status"] == "done"
    r = client.get(f"/api/watchlist/scan/job?job_id={job['job_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done" and body["matched"] == {str(wid): 1}
    assert body["by_source"] == {str(sid): 1} and body["auto_tagged"] == []
    assert body["scanned"] == body["total"] == 1
    assert client.get("/api/watchlist/scan/job?job_id=999999").status_code == 404
    assert client.post(f"/api/watchlist/scan/cancel?job_id={job['job_id']}").json() == {"cancelled": False}
    assert client.get("/api/watchlist").json()[0]["hit_count"] == 1
    # No body at all is a scan of everything.
    r = client.post("/api/watchlist/scan/start")
    assert r.status_code == 200
    assert store.wait_for_watchlist_scan_job(r.json()["job_id"], timeout=20)["matched"] == {wid: 1}


def test_a_running_scan_holds_idle_shutdown(client, store, write_csv):
    import server

    _ingest(store, write_csv, "i.csv", [["Cmd"], ["rclone copy"]])
    client.post("/api/watchlist", json={"value": "rclone", "kind": "filename"})
    gate = threading.Event()
    entered = threading.Event()
    real_unit = store._scan_unit

    def gated_unit(src, cols, ind):
        entered.set()
        gate.wait(10)
        return real_unit(src, cols, ind)

    store._scan_unit = gated_unit
    try:
        assert server._jobs_running() is False
        job = client.post("/api/watchlist/scan/start", json={}).json()
        assert entered.wait(5)
        assert job["status"] == "running"
        assert server._jobs_running() is True
        gate.set()
        store.wait_for_watchlist_scan_job(job["job_id"], timeout=20)
        assert server._jobs_running() is False
    finally:
        gate.set()
        del store._scan_unit
