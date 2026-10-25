"""A case that has been closed keeps nothing beside it: no `-wal`, no `-shm`.

SQLite removes those two itself, but only on the last close of a
connection that can write, and only when no other connection still has the
file open. Two things kept that from happening, and analysts found the
pairs beside their closed cases:

- Every read-only look at a closed case created them. `mode=ro` on a WAL
  database makes the -shm, and a -wal when there isn't one, and a
  read-only connection can never remove them. The home screen looks at
  every case in its list, so it left a pair beside each one, every time it
  was shown — and so did registering a case, a cross-case search and the
  watchlist's case picker.
- A close while a read was still open (a page fetch, a search-all count, a
  search-index build reading its table's row) was not the last close, so
  nothing checkpointed and the -wal kept every write since the last
  checkpoint beside the case.

Nothing here may lose a row that is still only in a -wal: these check the
rows reach the case file itself, not just that the files went away.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time

import pytest

from winnow import multicase as mc
from winnow import paths
from winnow import workspace as WS
from winnow.store import (DEFAULT_TAGS, Store, case_lock_path, fold_leftover_wal,
                          is_winnow_case_file)

ROWS = 50


def _beside(path) -> list[str]:
    return [s for s in ("-wal", "-shm") if os.path.exists(f"{path}{s}")]


def _sha(path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _closed_case(path, write_csv) -> tuple[int, int]:
    """A case with an import and three tagged rows, closed. (source, tag)"""
    st = Store(str(path), default_tags=DEFAULT_TAGS)
    rec = st.ingest_csv(write_csv([["a"], *[[str(i)] for i in range(ROWS)]],
                                  f"{os.path.basename(path)}.csv"), build_fts=False)
    tag = st.upsert_tag(None, "Suspicious", "#c94f4f", None)["id"]
    st.set_tags(rec["id"], [1, 2, 3], tag, True)
    st.close()
    return rec["id"], tag


def _tagged_in_the_file_alone(path, tmp_path) -> int:
    """Tagged rows in the case file without its -wal — what is left if the
    -wal is deleted. A copy, so nothing here touches the case itself."""
    alone = tmp_path / f"alone-{time.monotonic_ns()}.db"
    shutil.copyfile(path, alone)
    conn = sqlite3.connect(alone)
    try:
        return conn.execute("SELECT count(*) FROM row_tags").fetchone()[0]
    except sqlite3.OperationalError:   # not even the schema is in the file yet
        return 0
    finally:
        conn.close()


def _killed_mid_session(tmp_path, write_csv) -> str:
    """A case as a killed Winnow leaves it: the import and its tags are
    committed, but only to the -wal (64 KB pages: it folds itself into the
    case file at ~64 MB), and nothing closed it."""
    live = tmp_path / "live.db-winnow"
    st = Store(str(live), default_tags=DEFAULT_TAGS)
    try:
        rec = st.ingest_csv(write_csv([["a"], *[[str(i)] for i in range(ROWS)]], "k.csv"),
                            build_fts=False)
        tag = st.upsert_tag(None, "Suspicious", "#c94f4f", None)["id"]
        st.set_tags(rec["id"], [1, 2, 3], tag, True)
        dead = str(tmp_path / "killed.db-winnow")
        shutil.copyfile(live, dead)
        shutil.copyfile(f"{live}-wal", f"{dead}-wal")
    finally:
        st.close()
    assert _tagged_in_the_file_alone(dead, tmp_path) == 0, "expected the work to be in the -wal only"
    return dead


def _register(client, path, name):
    r = client.post("/api/cases", json={"path": str(path), "name": name})
    assert r.status_code == 200, r.text


# ------------------------------------------------- looking at a closed case

def test_the_home_screen_leaves_nothing_beside_the_cases_it_lists(client, tmp_path, write_csv):
    other = tmp_path / "host2.db-winnow"
    _closed_case(other, write_csv)
    assert _beside(other) == []
    _register(client, other, "host2")      # is_winnow_case_file looks first
    listed = {c["path"]: c for c in client.get("/api/cases").json()}
    assert listed[str(other)]["source_count"] == 1
    assert listed[str(other)]["row_count"] == ROWS
    assert client.get("/api/watchlist/cases").status_code == 200
    assert _beside(other) == []


def test_reading_another_case_leaves_nothing_beside_it(store, tmp_path, write_csv):
    other = tmp_path / "host2.db-winnow"
    src, _ = _closed_case(other, write_csv)
    assert is_winnow_case_file(str(other))
    with mc.CaseReader(str(other)) as r:
        assert [s["row_count"] for s in r.sources()] == [ROWS]
    assert mc.query_across([str(other)], f"SELECT count(*) FROM c1.src_{src}")["rows"] == [[ROWS]]
    assert mc.sweep_values([str(other)], ["7"])[0]["error"] is None
    store.import_watchlist_from_case(str(other))
    assert _beside(other) == []


def test_a_look_reads_rows_that_are_only_in_the_wal(tmp_path, write_csv):
    """A -wal beside the case means rows may live only there (a Winnow has
    it open, or was killed), so that look is a plain mode=ro one."""
    dead = _killed_mid_session(tmp_path, write_csv)
    assert is_winnow_case_file(dead)
    with mc.CaseReader(dead) as r:
        assert [s["row_count"] for s in r.sources()] == [ROWS]


def test_the_peek_uri_reads_the_file_as_it_stands_only_when_there_is_no_wal(tmp_path):
    wal_db = tmp_path / "w.db"
    c = sqlite3.connect(wal_db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (x)")
    c.commit()
    assert "immutable=1" not in paths.sqlite_peek_uri(wal_db)    # open: its -wal is there
    c.close()
    assert paths.sqlite_peek_uri(wal_db).endswith("?mode=ro&immutable=1")
    journal_db = tmp_path / "j.db"
    c = sqlite3.connect(journal_db)
    c.execute("CREATE TABLE t (x)")
    c.commit()
    c.close()
    assert paths.sqlite_peek_uri(journal_db).endswith("?mode=ro")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_the_peek_uri_finds_the_wal_of_a_case_reached_through_a_link(tmp_path):
    """SQLite names the -wal after the file a link points at, so looking
    beside the link alone read a live case without its newest rows (and
    refused to open a fresh case through a link as "not a case file").
    The look opens the target too: Windows SQLite names the -wal after the
    path it is given, so opening the link there missed it all the same."""
    wal_db = tmp_path / "w.db"
    link = tmp_path / "link.db"
    os.symlink(wal_db, link)
    c = sqlite3.connect(wal_db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (x)")
    c.commit()
    try:
        assert not os.path.exists(f"{link}-wal")
        uri = paths.sqlite_peek_uri(link)
        assert uri == paths.sqlite_ro_uri(os.path.realpath(wal_db))
        # The table is in the -wal alone (nothing has checkpointed it).
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as look:
            assert look.execute("SELECT name FROM sqlite_master").fetchall() == [("t",)]
    finally:
        c.close()


# ---------------------------------------------- a pair already left behind

def test_listing_a_case_folds_a_killed_winnows_wal_into_it(client, tmp_path, write_csv):
    dead = _killed_mid_session(tmp_path, write_csv)
    _register(client, dead, "killed")
    listed = {c["path"]: c for c in client.get("/api/cases").json()}
    assert listed[dead]["row_count"] == ROWS
    assert _beside(dead) == []
    assert _tagged_in_the_file_alone(dead, tmp_path) == 3


def test_a_leftover_wal_is_not_touched_while_a_winnow_holds_the_case(tmp_path, write_csv):
    dead = _killed_mid_session(tmp_path, write_csv)
    # A marker with a live heartbeat: another machine's Winnow, on a share.
    with open(case_lock_path(dead), "w", encoding="utf-8") as f:
        json.dump({"host": "other-box", "user": "analyst2", "pid": 999999,
                   "heartbeat_at": time.time()}, f)
    before = (_sha(dead), _sha(f"{dead}-wal"))
    assert fold_leftover_wal(dead) is False
    assert (_sha(dead), _sha(f"{dead}-wal")) == before


def test_a_wal_beside_a_file_that_is_not_a_case_is_never_written(tmp_path):
    """Registered paths are checked once, at registration; evidence put
    at one since must not be opened read-write."""
    ev = tmp_path / "History"
    keep = sqlite3.connect(ev)
    keep.execute("PRAGMA journal_mode=WAL")
    keep.execute("PRAGMA wal_autocheckpoint=0")
    keep.execute("CREATE TABLE urls (url TEXT)")
    keep.execute("INSERT INTO urls VALUES ('https://example.test/')")
    keep.commit()
    dead = tmp_path / "History-copy"
    shutil.copyfile(ev, dead)
    shutil.copyfile(f"{ev}-wal", f"{dead}-wal")
    keep.close()
    before = (_sha(dead), _sha(f"{dead}-wal"))
    assert fold_leftover_wal(str(dead)) is False
    assert (_sha(dead), _sha(f"{dead}-wal")) == before


# ------------------------------------------------------- closing the case

def _hold_a_reader(st, src, release: threading.Event, holding: threading.Event):
    def run():
        with st._reader() as ro:
            cur = ro.execute(f"SELECT * FROM src_{src}")
            cur.fetchone()      # mid-statement, the way a streaming export holds it
            holding.set()
            release.wait(10)
            cur.close()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert holding.wait(10)
    return t


def test_closing_waits_for_a_read_still_in_flight(tmp_path, write_csv):
    """A search-all count or a page fetch still reading when the case
    closed made the writer's close not the last one, so SQLite left both
    files — and every write since the last checkpoint in the -wal."""
    path = tmp_path / "case.db-winnow"
    src, _ = _closed_case(path, write_csv)
    st = Store(str(path), default_tags=DEFAULT_TAGS)
    release, holding = threading.Event(), threading.Event()
    t = _hold_a_reader(st, src, release, holding)
    threading.Timer(0.3, release.set).start()
    st.close()
    t.join(10)
    assert _beside(path) == []


def test_a_read_that_outlasts_the_close_finds_every_write_already_in_the_case_file(
        tmp_path, write_csv, monkeypatch):
    """A download can run past the close, and isn't cut off. Then the close
    can't be the last one, so it folds the -wal in itself: deleting what is
    left beside the case loses nothing, and listing the case on the home
    screen removes it."""
    monkeypatch.setattr(Store, "FOLD_TIMEOUT_S", 0.2)
    path = tmp_path / "case.db-winnow"
    st = Store(str(path), default_tags=DEFAULT_TAGS)
    rec = st.ingest_csv(write_csv([["a"], *[[str(i)] for i in range(ROWS)]]), build_fts=False)
    tag = st.upsert_tag(None, "Suspicious", "#c94f4f", None)["id"]
    st.set_tags(rec["id"], [1, 2, 3], tag, True)
    release, holding = threading.Event(), threading.Event()
    t = _hold_a_reader(st, rec["id"], release, holding)
    try:
        st.close()
        assert _tagged_in_the_file_alone(path, tmp_path) == 3
    finally:
        release.set()
        t.join(10)
    assert fold_leftover_wal(str(path)) is True
    assert _beside(path) == []
    assert _tagged_in_the_file_alone(path, tmp_path) == 3


def test_switching_cases_leaves_nothing_beside_the_one_left(client, tmp_path, write_csv):
    a, b = tmp_path / "a.db-winnow", tmp_path / "b.db-winnow"
    _closed_case(a, write_csv)
    _closed_case(b, write_csv)
    WS.cases.create(str(a), name="a")
    WS.cases.create(str(b), name="b")
    assert client.post("/api/case/open", json={"path": str(a)}).status_code == 200
    src = client.get("/api/sources").json()[0]["id"]
    assert client.post("/api/view", json={"source_id": src, "filters": [],
                                          "sort": [{"column": "a", "dir": "desc"}]}).status_code == 200
    assert client.post("/api/case/open", json={"path": str(b)}).status_code == 200
    client.get("/api/cases")      # back on the home screen
    assert _beside(a) == []
