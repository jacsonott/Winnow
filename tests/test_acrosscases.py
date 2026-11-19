"""Across cases on the open case's own backend (winnow/acrosscases.py).

Two things are under test. The boundary: another case is read by a Store
opened read_only — no lock taken, nothing left beside the file, a write
refused by SQLite itself, a case another Winnow holds still readable — and
the one write there is (a tag) goes to the case that owns the row, only
when that case is free. And the shape: the sweep, the Timeline and a
query all answer in Timeline lines that carry their case, read the way
that case's own Timeline reads them (shipped summaries, configured
types, tags by name).
"""

from __future__ import annotations

import csv
import os
import sqlite3
import time

import pytest

from winnow import acrosscases as ac
from winnow.store import DEFAULT_TAGS, Store, probe_case_lock

# EvtxECmd's own header set, so the shipped summary applies.
EVTX = ["RecordNumber", "EventRecordId", "TimeCreated", "EventId", "Level", "Provider", "Channel",
        "ProcessId", "ThreadId", "Computer", "ChunkNumber", "UserId", "MapDescription", "UserName",
        "RemoteHost", "PayloadData1", "PayloadData2", "PayloadData3", "PayloadData4", "PayloadData5",
        "PayloadData6", "ExecutableInfo", "HiddenRecord", "SourceFile", "Keywords", "ExtraDataOffset",
        "Payload"]


def evtx_row(n, when, eid, host, user, desc, remote="", exe=""):
    row = {c: "" for c in EVTX}
    row.update(RecordNumber=str(n), EventRecordId=str(n), TimeCreated=when, EventId=eid,
               Provider="Microsoft-Windows-Security-Auditing", Channel="Security", Computer=host,
               MapDescription=desc, UserName=user, RemoteHost=remote, ExecutableInfo=exe)
    return [row[c] for c in EVTX]


HOSTS = {
    "WKSTN-014": [
        evtx_row(1, "2026-03-13 01:52:28", "4625", "WKSTN-014", "svc_backup", "Failed logon", "10.0.0.5"),
        evtx_row(2, "2026-03-13 02:00:03", "4624", "WKSTN-014", "svc_backup", "Successful logon", "10.0.0.5"),
        evtx_row(3, "2026-03-13 02:04:10", "4688", "WKSTN-014", "svc_backup", "Process creation", "",
                 "C:\\Users\\Public\\evil.exe"),
    ],
    "SRV-DC01": [
        evtx_row(1, "2026-03-13 02:10:00", "4624", "SRV-DC01", "svc_backup", "Successful logon", "10.0.0.14"),
        evtx_row(2, "2026-03-13 03:00:00", "4624", "SRV-DC01", "alice", "Successful logon", "10.0.0.20"),
    ],
    "WKSTN-022": [
        evtx_row(1, "2026-03-13 01:00:00", "4624", "WKSTN-022", "bob", "Successful logon", "10.0.0.30"),
    ],
}


def _make_case(tmp_path, host, rows, tag_rids=()):
    csv_path = tmp_path / f"{host}_EvtxECmd_Output.csv"
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(EVTX)
        w.writerows(rows)
    db = tmp_path / f"{host}.db-winnow"
    st = Store(str(db), default_tags=DEFAULT_TAGS)
    st.ingest_csv(str(csv_path), name=csv_path.name, build_fts=False)
    tag = st.list_tags()[0]
    if tag_rids:
        st.set_tags(1, list(tag_rids), tag["id"], True)
    st.close()
    return str(db)


@pytest.fixture
def cases(tmp_path):
    """Three closed host cases; WKSTN-014's logon and process and DC01's
    first logon are tagged as findings."""
    return [
        _make_case(tmp_path, "WKSTN-014", HOSTS["WKSTN-014"], tag_rids=(2, 3)),
        _make_case(tmp_path, "SRV-DC01", HOSTS["SRV-DC01"], tag_rids=(1,)),
        _make_case(tmp_path, "WKSTN-022", HOSTS["WKSTN-022"]),
    ]


NAMES = {}


def _names(paths):
    return {p: os.path.basename(p).split(".")[0] for p in paths}


# ------------------------------------------------------------ the visitor

def test_a_visitor_takes_no_lock_and_leaves_nothing_beside_the_case(cases):
    before = sorted(os.listdir(os.path.dirname(cases[0])))
    with ac.visit(cases[0]) as st:
        assert probe_case_lock(cases[0]) is None
        assert [s["row_count"] for s in st.list_sources()] == [3]
    assert sorted(os.listdir(os.path.dirname(cases[0]))) == before


def test_a_visitor_cannot_write_the_case(cases):
    """Not a promise: SQLite refuses it, whatever a caller tries."""
    with ac.visit(cases[0]) as st:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            st.db.execute("UPDATE sources SET name='hacked'")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            st.db.execute("DELETE FROM row_tags")
    with sqlite3.connect(cases[0]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM row_tags").fetchone()[0] == 2


def test_a_visitor_builds_no_index_in_the_case_it_reads(cases):
    """Search all on the open case starts a trigram build for a table
    without one; reading another case must not."""
    with ac.visit(cases[0]) as st:
        ac.sweep_case(st, ["evil.exe"], {}, case="x", path=cases[0])
        assert not st._fts_threads and not st._index_threads
    with sqlite3.connect(cases[0]) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        indexed = conn.execute("SELECT COUNT(*) FROM sources WHERE has_fts=1").fetchone()[0]
    assert "fts_1" not in names and indexed == 0


def test_a_case_another_winnow_has_open_is_still_read(tmp_path):
    """The normal state when someone works a set of hosts: the case is
    held, WAL and all, and its latest tags are in the -wal."""
    path = _make_case(tmp_path, "LIVE", HOSTS["SRV-DC01"])
    live = Store(path, default_tags=DEFAULT_TAGS)
    try:
        live.set_tags(1, [2], live.list_tags()[0]["id"], True)
        lines, n = ac.timeline_case(_visit_store(path), {}, case="LIVE", path=path)
        assert n == 1 and lines[0]["rid"] == 2
        assert probe_case_lock(path) is not None      # still the other Winnow's
    finally:
        live.close()


def _visit_store(path):
    st = Store(path, read_only=True)
    _OPEN.append(st)
    return st


_OPEN: list = []


@pytest.fixture(autouse=True)
def _close_visitors():
    yield
    while _OPEN:
        _OPEN.pop().close()


def test_an_old_case_is_refused_by_name_not_read_wrong(tmp_path):
    db = tmp_path / "old.db-winnow"
    with sqlite3.connect(db) as conn:
        conn.executescript("CREATE TABLE sources(id INTEGER PRIMARY KEY, name TEXT, table_name TEXT);"
                           "CREATE TABLE row_tags(source_id, rid, tag_id);"
                           "CREATE TABLE tag_defs(id INTEGER PRIMARY KEY, name TEXT);")
    with pytest.raises(ac.NotVisitable):
        with ac.visit(str(db)):
            pass


# ----------------------------------------------------- one shape: lines

def test_lines_read_the_way_the_cases_own_timeline_reads_them(cases):
    with ac.visit(cases[0]) as st:
        lines = ac.lines_of(st, {}, [(1, 3), (1, 1)], case="WKSTN-014", path=cases[0])
    assert [ln["rid"] for ln in lines] == [1, 3]                  # time order, not the order asked
    first = lines[0]
    assert first["case"] == "WKSTN-014" and first["path"] == cases[0]
    assert first["ts"] == "2026-03-13 01:52:28"
    assert first["kind"] == "Event logs"                          # the shipped nickname, tool dropped
    assert first["lead"] == "Failed logon"                        # the shipped EvtxECmd summary
    assert "svc_backup" in first["detail"] and "id 4625" in first["detail"]
    assert lines[1]["tags"] and lines[1]["tags"][0]["name"] == "TA"   # by name: ids are per case


def test_a_configured_type_label_travels_to_every_case(cases):
    """Configure tables… is per header set and machine-wide, so the label
    an analyst set reads the same on every host's lines."""
    with ac.visit(cases[1]) as st:
        configs = {1: {"type_label": "Security log", "timestamp_column": None, "body_columns": []}}
        lines = ac.lines_of(st, configs, [(1, 1)], case="SRV-DC01", path=cases[1])
    assert lines[0]["kind"] == "Security log"


# ------------------------------------------------------------------ sweep

def test_the_sweep_answers_per_case_per_table_per_term_with_lines(cases):
    names = _names(cases)
    jobs = ac.SweepJobs()
    job = jobs.start(cases, ["svc_backup", "evil.exe"], names=names, configs_for=lambda st: {})
    job = _wait(jobs, job["job_id"])
    by_case = {c["case"]: c for c in job["cases"]}
    wk = by_case["WKSTN-014"]["tables"][0]
    assert wk["match_count"] == 3
    assert {t["term"]: t["match_count"] for t in wk["terms"]} == {"svc_backup": 3, "evil.exe": 1}
    hit = next(ln for ln in wk["lines"] if ln["rid"] == 3)
    assert set(hit["matched"]) == {"svc_backup", "evil.exe"}
    assert hit["lead"] == "Process creation" and hit["case"] == "WKSTN-014"
    assert by_case["SRV-DC01"]["tables"][0]["match_count"] == 1
    assert by_case["WKSTN-022"]["tables"] == []
    assert job["hits"] == 4 and job["read"] == job["total"] == 3


def test_one_unreadable_case_does_not_sink_the_sweep(cases, tmp_path):
    junk = tmp_path / "junk.db-winnow"
    junk.write_bytes(b"not a database")
    paths = [cases[0], str(junk)]
    jobs = ac.SweepJobs()
    job = _wait(jobs, jobs.start(paths, ["evil.exe"], names=_names(paths), configs_for=lambda st: {})["job_id"])
    assert job["cases"][0]["tables"] and job["cases"][1]["status"] == "error"


def test_a_new_sweep_replaces_the_running_one(cases):
    jobs = ac.SweepJobs()
    first = jobs.start(cases, ["svc_backup"], names=_names(cases), configs_for=lambda st: {})
    second = jobs.start(cases, ["alice"], names=_names(cases), configs_for=lambda st: {})
    assert jobs.get(first["job_id"]) is None
    assert _wait(jobs, second["job_id"])["hits"] == 1


def _wait(jobs, job_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = jobs.get(job_id)
        if job and job["done"]:
            return job
        time.sleep(0.05)
    raise AssertionError("sweep did not finish")


# --------------------------------------------------------------- timeline

def test_the_timeline_interleaves_every_cases_findings_in_time_order(cases):
    out = ac.timeline_across(cases, names=_names(cases), configs_for=lambda st: {})
    got = [(ln["case"], ln["ts"]) for ln in out["lines"]]
    assert got == [("WKSTN-014", "2026-03-13 02:00:03"), ("WKSTN-014", "2026-03-13 02:04:10"),
                   ("SRV-DC01", "2026-03-13 02:10:00")]
    assert out["total"] == 3 and not out["truncated"]
    # The gap is to the line above in the MERGED list: 02:04:10 → 02:10:00.
    assert out["lines"][2]["gap"] == 350
    assert {c["case"]: c["findings"] for c in out["per_case"]} == {
        "WKSTN-014": 2, "SRV-DC01": 1, "WKSTN-022": 0}


def test_the_timeline_keeps_a_window_and_says_when_it_is_cut(cases):
    out = ac.timeline_across(cases, names=_names(cases), configs_for=lambda st: {},
                             start="2026-03-13 02:01:00", end="2026-03-13 02:30:00")
    assert [ln["ts"] for ln in out["lines"]] == ["2026-03-13 02:04:10", "2026-03-13 02:10:00"]
    cut = ac.timeline_across(cases, names=_names(cases), configs_for=lambda st: {}, limit=2)
    assert len(cut["lines"]) == 2 and cut["truncated"]
    with pytest.raises(ValueError):
        ac.timeline_across(cases, names=_names(cases), configs_for=lambda st: {}, start="not a time")


def test_the_timeline_filters_tags_by_name(cases):
    out = ac.timeline_across(cases, names=_names(cases), configs_for=lambda st: {}, tag_names=["Benign"])
    assert out["lines"] == []


# ------------------------------------------------------------------ query

def test_a_query_reading_one_table_comes_back_as_lines(cases):
    from winnow import multicase as mc
    sql = "SELECT rid, UserName FROM c2.src_1 WHERE UserName = 'svc_backup'"
    res = mc.query_across(cases, sql, names=_names(cases))
    lines = ac.query_lines(res, sql, names=_names(cases), configs_for=lambda st: {})
    assert [(ln["case"], ln["rid"], ln["lead"]) for ln in lines] == [("SRV-DC01", 1, "Successful logon")]


def test_a_query_across_cases_names_its_rows_with_case_and_source_id(cases):
    from winnow import multicase as mc
    sql = ("SELECT 'c1' AS \"case\", 1 AS source_id, rid FROM c1.src_1 WHERE EventId = '4624' "
           "UNION ALL SELECT 'c2', 1, rid FROM c2.src_1 WHERE EventId = '4624'")
    res = mc.query_across(cases, sql, names=_names(cases))
    lines = ac.query_lines(res, sql, names=_names(cases), configs_for=lambda st: {})
    assert [ln["case"] for ln in lines] == ["WKSTN-014", "SRV-DC01", "SRV-DC01"]


def test_an_aggregate_stays_a_table(cases):
    from winnow import multicase as mc
    sql = "SELECT EventId, COUNT(*) FROM c1.src_1 GROUP BY 1"
    res = mc.query_across(cases, sql, names=_names(cases))
    assert ac.query_lines(res, sql, names=_names(cases), configs_for=lambda st: {}) is None


# ------------------------------------------------------------------ routes

@pytest.fixture
def registered(cases):
    from winnow import workspace as WS
    for p in cases:
        WS.cases.create(p, name=os.path.basename(p).split(".")[0])
    return cases


def test_routes_read_registered_cases_only(client, registered, tmp_path):
    stranger = _make_case(tmp_path, "STRANGER", HOSTS["WKSTN-022"])
    r = client.post("/api/across/timeline", json={"paths": [stranger]})
    assert r.status_code == 400 and "registered" in r.json()["detail"]


def test_the_case_list_says_which_another_winnow_holds(client, registered):
    live = Store(registered[1], default_tags=DEFAULT_TAGS)
    try:
        body = {c["path"]: c for c in client.get("/api/across/cases").json()["cases"]}
        assert body[registered[1]]["held_by"] and not body[registered[0]]["held_by"]
    finally:
        live.close()


def test_tagging_a_hit_writes_it_into_the_case_that_owns_it(client, registered):
    path = registered[2]                                  # WKSTN-022: nothing tagged yet
    row = client.get("/api/across/row", params={"path": path, "source_id": 1, "rid": 1}).json()
    assert row["tags"] == [] and [t["name"] for t in row["case_tags"]] == ["TA", "Suspicious", "Benign"]
    ta = row["case_tags"][0]["id"]
    r = client.post("/api/across/tag", json={"path": path, "source_id": 1, "rid": 1, "tag_id": ta})
    assert r.status_code == 200 and [t["name"] for t in r.json()["tags"]] == ["TA"]
    assert probe_case_lock(path) is None                  # opened for the write, and let go
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT source_id, rid, tag_id FROM row_tags").fetchall() == [(1, 1, ta)]
    out = client.post("/api/across/timeline", json={"paths": [path]}).json()
    assert [ln["rid"] for ln in out["lines"]] == [1]      # now a finding on the cross-case Timeline
    client.post("/api/across/tag", json={"path": path, "source_id": 1, "rid": 1, "tag_id": ta, "on": False})
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM row_tags").fetchone()[0] == 0


def test_tagging_in_a_case_another_winnow_holds_is_refused_by_name(client, registered):
    live = Store(registered[1], default_tags=DEFAULT_TAGS)
    try:
        r = client.post("/api/across/tag", json={"path": registered[1], "source_id": 1, "rid": 2,
                                                 "tag_id": 1})
        assert r.status_code == 409 and r.json()["detail"]["error"] == "case_in_use"
        assert "another Winnow" in r.json()["detail"]["message"]
    finally:
        live.close()
    with sqlite3.connect(registered[1]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM row_tags WHERE rid=2").fetchone()[0] == 0


def test_the_open_case_answers_through_its_own_store(client, registered, store, tmp_path):
    """The open case is part of the answer, read by the Store that has
    it open — its tags as they are this second."""
    csv_path = tmp_path / "here.csv"
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(EVTX)
        w.writerow(evtx_row(1, "2026-03-13 02:05:00", "4688", "HERE", "svc_backup", "Process creation"))
    store.ingest_csv(str(csv_path), name="here.csv", build_fts=False)
    store.set_tags(1, [1], store.list_tags()[0]["id"], True)
    out = client.post("/api/across/timeline", json={}).json()
    assert [ln["ts"] for ln in out["lines"]] == [
        "2026-03-13 02:00:03", "2026-03-13 02:04:10", "2026-03-13 02:05:00", "2026-03-13 02:10:00"]
    job = client.post("/api/across/sweep", json={"values": ["svc_backup"]}).json()
    deadline = time.time() + 20
    while not job["done"] and time.time() < deadline:
        time.sleep(0.05)
        job = client.get(f"/api/across/sweep/{job['job_id']}").json()
    here = next(c for c in job["cases"] if c["is_here"])
    assert here["tables"][0]["match_count"] == 1
