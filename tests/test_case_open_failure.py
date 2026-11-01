"""A case file that will not open, chosen while another case is open.

/api/case/open opens the new Store before it lets go of the old one, so a
failure leaves the analyst where they were: the open case stays open and
answers. That branch had never run. Running it found what the failed open
itself left behind: the case lock it had already taken (whose heartbeat
thread kept the half-built Store alive), its writer connection and its
views file, all for the life of the process. So the file read as open in
this same Winnow, a second try answered "in use" by itself, and each try
stranded a views file the startup sweep cannot take from a live process.

The file used is one is_winnow_case_file accepts (it has a case's three
tables) and Store cannot open (not their columns): the realistic shape is
a damaged case, or one written by something else that borrowed the names.
"""

from __future__ import annotations

import os
import sqlite3
import threading

import pytest

import server
from winnow import store as store_module
from winnow import workspace as WS
from winnow.store import (VIEWS_PREFIX, Store, case_lock_path, is_winnow_case_file,
                          probe_case_lock)


@pytest.fixture(autouse=True)
def views_dir(tmp_path, monkeypatch):
    """Views files made here go where nothing else puts one: /dev/shm is
    shared with every other Winnow and test run on the machine, so a
    before-and-after listing of it compares their files too."""
    d = tmp_path / "views"
    d.mkdir()
    monkeypatch.setattr(store_module, "_preferred_views_dir", lambda: str(d))
    return d


@pytest.fixture
def broken_case(tmp_path):
    d = tmp_path / "elsewhere"
    d.mkdir()
    p = d / "damaged.db-winnow"
    c = sqlite3.connect(p)
    c.executescript("CREATE TABLE sources(x); CREATE TABLE tag_defs(x); CREATE TABLE row_tags(x);")
    c.close()
    assert is_winnow_case_file(str(p)), "the fixture has to get past the not-a-case check"
    return p


def _views_files() -> set[str]:
    return {n for n in os.listdir(store_module._preferred_views_dir()) if n.startswith(VIEWS_PREFIX)}


def _lock_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "winnow-case-lock" and t.is_alive()]


def _nothing_left_behind(case, views_before, threads_before):
    assert probe_case_lock(str(case)) is None, "the failed open still holds the case lock"
    assert not os.path.exists(case_lock_path(str(case)))
    assert sorted(x.name for x in case.parent.iterdir()) == [case.name], \
        "a -wal/-shm or lock file was left beside the case"
    assert _views_files() == views_before, "a views file was stranded"
    assert len(_lock_threads()) == threads_before, "a heartbeat thread was left running"


def test_a_store_that_fails_to_open_gives_back_what_it_took(broken_case):
    views_before, threads_before = _views_files(), len(_lock_threads())
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        Store(str(broken_case))
    _nothing_left_behind(broken_case, views_before, threads_before)


def test_a_case_that_will_not_open_leaves_the_open_case_open(client, store, tmp_path, broken_case):
    src = tmp_path / "rows.csv"
    src.write_text("Host,EventId\nWKS1,4624\nWKS2,4625\n", encoding="utf-8")
    sid = store.ingest_csv(str(src), name="rows.csv")["id"]
    views_before, threads_before = _views_files(), len(_lock_threads())

    r = client.post("/api/case/open", json={"path": str(broken_case)})
    assert r.status_code == 400
    assert r.json()["detail"] == "Could not open case: no such column: source_id"

    # Where the analyst was, still open and still answering: reads, a
    # write, and the case the page is told it is in.
    assert server.STORE is store and not store.closed
    assert [s["name"] for s in client.get("/api/sources").json()] == ["rows.csv"]
    tag = store.list_tags()[0]
    w = client.post("/api/row_tags", json={"source_id": sid, "rids": [1], "tag_id": tag["id"], "on": True})
    assert w.status_code == 200, w.text
    assert client.get(f"/api/tags?source_id={sid}").json()["counts"][str(tag["id"])] == 1
    cur = client.get("/api/case/current").json()
    assert cur["open"] and os.path.abspath(cur["path"]) == os.path.abspath(store.path)
    # Not added to the case list, and nothing of the attempt left behind.
    assert WS.cases.find_by_path(str(broken_case)) is None
    _nothing_left_behind(broken_case, views_before, threads_before)


def test_trying_again_is_refused_for_the_same_reason_not_as_in_use(client, broken_case):
    """The lock the first try stranded made the second one a 409 naming
    this very process as the holder."""
    for _ in range(2):
        r = client.post("/api/case/open", json={"path": str(broken_case)})
        assert r.status_code == 400, r.text
        assert "no such column" in r.json()["detail"]
