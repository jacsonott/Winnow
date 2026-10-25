"""build_view's parallel Contains scan (Store._parallel_contains): the
same rows, in the same order, as the single scan it replaces, on a table
and on a merge; the fallbacks back to that scan; cancel; and that the
scan no longer holds the writer lock while it reads."""

from __future__ import annotations

import threading

import pytest

import winnow.store as store_mod
from winnow.store import OpCancelled

HEADER = ["Host", "User", "Msg", "Path"]


def _rows(n: int = 600) -> list[list[str]]:
    rows = [HEADER]
    for i in range(n):
        msg = "logon ok"
        if i % 97 == 5:
            msg = "beacon to 10.0.0.%d" % i
        elif i % 89 == 7:
            msg = "Error cmd.exe exited"
        elif i % 83 == 3:
            msg = "disk 50% full"
        elif i % 79 == 1:
            msg = "key a_b set"
        elif i % 71 == 2:
            msg = "Администратор вошёл"
        rows.append([f"HOST-{i % 7}", f"user{i % 13}", msg,
                     f"C:\\users\\jacso{i % 5}\\file{i}.txt" if i % 11 == 0 else "C:\\Windows\\System32"])
    return rows


TERMS = ["beacon", "BEACON", "jacso3", "50%", "a_b", "Админ", "админ", "error cmd", "HOST-3", "zzqqxx"]


@pytest.fixture
def parallel(store, monkeypatch):
    """Every table qualifies, four threads, and a record of what each
    parallel scan returned (None = it handed back to the usual scan). No
    search index: the first search would start one in the background, and
    a search after it lands asks the index instead of scanning."""
    store._ensure_fts_building = lambda *a, **k: None
    monkeypatch.setattr(store_mod, "PARALLEL_SCAN_MIN_ROWS", 0)
    monkeypatch.setattr(store_mod, "PARALLEL_SCAN_THREADS", 4)
    calls: list = []
    real = store_mod.Store._parallel_contains

    def spy(self, *a, **k):
        out = real(self, *a, **k)
        calls.append(out)
        return out
    monkeypatch.setattr(store_mod.Store, "_parallel_contains", spy)
    return calls


def _view_cells(store, source_id, spec):
    v = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": [], **spec})
    return [r["cells"] for r in store.fetch_rows(v["view_id"], 0, 10_000)["rows"]]


def _single(monkeypatch, fn):
    """fn's answer from the single scan, the way a small table gets it."""
    with monkeypatch.context() as m:
        m.setattr(store_mod.Store, "_parallel_contains", lambda self, *a, **k: None)
        return fn()


SPECS = [
    {},
    {"sort": [{"column": "Host", "dir": "desc"}]},
    {"filters": [{"column": "User", "op": "equals", "value": "user3"}]},
]


@pytest.mark.parametrize("term", TERMS)
def test_same_rows_and_order_as_the_single_scan(store, write_csv, parallel, monkeypatch, term):
    sid = store.ingest_csv(write_csv(_rows()), name="t.csv", build_fts=False)["id"]
    for extra in SPECS:
        spec = {"search": term, "search_mode": "contains", **extra}
        got = _view_cells(store, sid, spec)
        want = _single(monkeypatch, lambda: _view_cells(store, sid, spec))
        assert got == want, (term, extra)
    assert len(parallel) == len(SPECS) and all(c is not None for c in parallel), "each should have run in parallel"


def test_a_term_on_every_row_still_runs_in_parallel(store, write_csv, parallel, monkeypatch):
    # "C:\" is in every row's Path. Carried back whole: only the count cap
    # hands back (below), not how many of the rows read matched.
    sid = store.ingest_csv(write_csv(_rows()), name="t.csv", build_fts=False)["id"]
    spec = {"search": "C:\\", "search_mode": "contains"}
    got = _view_cells(store, sid, spec)
    assert len(parallel) == 1 and parallel[0] is not None
    assert len(got) == 600
    assert got == _single(monkeypatch, lambda: _view_cells(store, sid, spec))


def test_more_matches_than_the_cap_hands_back_to_the_single_scan(store, write_csv, parallel, monkeypatch):
    monkeypatch.setattr(store_mod, "PARALLEL_SCAN_MAX_RIDS", 3)
    sid = store.ingest_csv(write_csv(_rows()), name="t.csv", build_fts=False)["id"]
    spec = {"search": "beacon", "search_mode": "contains"}
    got = _view_cells(store, sid, spec)
    assert parallel == [None]
    assert len(got) == 7
    assert got == _single(monkeypatch, lambda: _view_cells(store, sid, spec))


def test_a_merge_whose_member_spells_a_column_differently(store, write_csv, parallel, monkeypatch):
    rows = _rows(300)
    a = store.ingest_csv(write_csv(rows, "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["HOST", *HEADER[1:]], *rows[1:]], "b.csv"), name="b", build_fts=False)["id"]
    mid = store.create_merge("m", [a, b])["id"]
    for term in ("beacon", "HOST-3", "jacso"):
        spec = {"search": term, "search_mode": "contains"}
        got = _view_cells(store, mid, spec)
        assert got == _single(monkeypatch, lambda: _view_cells(store, mid, spec)), term
        assert got
    assert len(parallel) == 6 and all(c is not None for c in parallel)


def test_spec_sql_still_renders_the_scan_itself(store, write_csv, parallel):
    sid = store.ingest_csv(write_csv(_rows()), name="t.csv", build_fts=False)["id"]
    sql = store.spec_sql(sid, {"source_id": sid, "filters": [], "sort": [], "search": "beacon",
                               "search_mode": "contains"})
    assert "LIKE" in sql and "json_each" not in sql
    assert parallel == []


@pytest.fixture
def held_scan(store, monkeypatch):
    """A parallel scan whose threads stop at their first row until the test
    releases them: `started` is set once one is there. The check is
    _contains_scan's plus a function that waits, registered on every reader.
    Polls for a cancel on every VM step, so one landing during the wait is
    seen the moment the wait ends."""
    store._ensure_fts_building = lambda *a, **k: None
    started, release = threading.Event(), threading.Event()
    rows_read = []

    def hold(_rid):
        rows_read.append(1)
        started.set()
        release.wait(10)
        return 1

    real_open = store_mod.Store._open_reader

    def open_reader(self):
        conn = real_open(self)
        conn.create_function("HOLD_SCAN", 1, hold)
        return conn
    monkeypatch.setattr(store_mod.Store, "_open_reader", open_reader)
    real_scan = store_mod._contains_scan
    monkeypatch.setattr(store_mod, "_contains_scan",
                        lambda cols, term: ((f"(HOLD_SCAN(rid) AND {real_scan(cols, term)[0]})",
                                             real_scan(cols, term)[1])))
    monkeypatch.setattr(store_mod, "PARALLEL_SCAN_MIN_ROWS", 0)
    monkeypatch.setattr(store_mod, "PARALLEL_SCAN_THREADS", 4)
    monkeypatch.setattr(store_mod, "PARALLEL_SCAN_POLL_OPS", 1)
    yield started, release, rows_read
    release.set()


def _build_in_thread(store, sid, spec):
    out: dict = {}

    def run():
        try:
            out["view"] = store.build_view(sid, spec)
        except BaseException as e:  # noqa: BLE001 — handed to the test
            out["error"] = e
    t = threading.Thread(target=run)
    t.start()
    return t, out


def test_the_scan_does_not_hold_the_writer_lock(store, write_csv, held_scan):
    started, release, _ = held_scan
    sid = store.ingest_csv(write_csv(_rows()), name="t.csv", build_fts=False)["id"]
    t, out = _build_in_thread(store, sid, {"source_id": sid, "filters": [], "sort": [],
                                           "search": "beacon", "search_mode": "contains"})
    assert started.wait(10)
    # A tag write, an undo or another table's build waits on this lock;
    # the single scan held it for the whole search.
    assert store.lock.acquire(timeout=5)
    store.lock.release()
    release.set()
    t.join(30)
    assert "error" not in out and out["view"]["row_count"] == 7


def test_a_cancel_stops_the_scan_and_keeps_the_previous_view(store, write_csv, held_scan):
    started, release, rows_read = held_scan
    sid = store.ingest_csv(write_csv(_rows()), name="t.csv", build_fts=False)["id"]
    before = store.build_view(sid, {"source_id": sid, "filters": [{"column": "User", "op": "equals",
                                                                   "value": "user3"}], "sort": []})
    t, out = _build_in_thread(store, sid, {"source_id": sid, "filters": [], "sort": [], "op_token": "tok-1",
                                           "search": "beacon", "search_mode": "contains"})
    assert started.wait(10)
    assert store.cancel_op("tok-1")
    release.set()
    t.join(30)
    assert isinstance(out.get("error"), OpCancelled)
    # Stopped where it was: the build would refuse a cancelled token
    # anyway, so the rows read are what show the scan itself stopped
    # (each thread finishes the row it is on; uncancelled, it is all 600).
    assert len(rows_read) < 50
    assert store.fetch_rows(before["view_id"], 0, 5)["rows"]


def _half_indexed(store, sid, monkeypatch, rows: int = 250) -> None:
    """An index build stopped after its first `rows` rows, the way a case
    closed mid-build leaves one: has_fts 0, fts_progress at rows + 1."""
    with monkeypatch.context() as m:
        m.setattr(store_mod, "FTS_COMMIT_FIRST_ROWS", rows)

        def stop(n, held):
            store._closing = True
            return n
        m.setattr(store_mod, "_next_fts_commit_rows", stop)
        try:
            store.build_fts(sid)
        finally:
            store._closing = False
    assert store.get_source(sid)["has_fts"] == 0
    assert store._fts_partial_point(store._source_lite(sid)) == rows + 1


@pytest.fixture
def partial_points(monkeypatch):
    seen: list = []
    real = store_mod.Store._fts_partial_point

    def spy(self, src, conn=None):
        out = real(self, src, conn)
        seen.append(out)
        return out
    monkeypatch.setattr(store_mod.Store, "_fts_partial_point", spy)
    return seen


@pytest.mark.parametrize("term", TERMS)
def test_a_build_in_progress_answers_the_rows_it_has_indexed(store, write_csv, parallel, monkeypatch,
                                                            partial_points, term):
    if not store.search_index_supported():
        pytest.skip("no trigram index on this SQLite")
    sid = store.ingest_csv(write_csv(_rows()), name="t.csv", build_fts=False)["id"]
    _half_indexed(store, sid, monkeypatch)
    partial_points.clear()
    spec = {"search": term, "search_mode": "contains"}
    got = _view_cells(store, sid, spec)
    assert got == _single(monkeypatch, lambda: _view_cells(store, sid, spec))
    assert parallel[-1] is not None
    if len(term) >= store_mod.TRIGRAM_MIN_LEN:
        # Asked once to plan the ranges and once more inside the index
        # read's own transaction.
        assert partial_points == [251, 251]
    else:
        assert partial_points == []


def test_an_index_started_over_since_hands_back_to_the_single_scan(store, write_csv, parallel, monkeypatch):
    """The rows below the point were planned as indexed; if the index the
    MATCH reads has fewer (a fresh build dropped it), they'd be missed."""
    if not store.search_index_supported():
        pytest.skip("no trigram index on this SQLite")
    sid = store.ingest_csv(write_csv(_rows()), name="t.csv", build_fts=False)["id"]
    _half_indexed(store, sid, monkeypatch)
    answers = iter([251, None])
    monkeypatch.setattr(store_mod.Store, "_fts_partial_point", lambda self, src, conn=None: next(answers))
    spec = {"search": "beacon", "search_mode": "contains"}
    got = _view_cells(store, sid, spec)
    assert parallel == [None]
    assert len(got) == 7
