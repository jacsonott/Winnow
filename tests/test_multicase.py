"""Reading several cases at once: the IOC sweep, cross-case SQL and the
unified timeline (winnow/multicase.py).

The design under test is one writable case plus N read-only readers, so
these check the boundary as much as the feature: nothing here takes
another case's lock, nothing writes, and a case another Winnow has open
is still readable — which is the normal state when someone is working a
set of hosts.
"""

from __future__ import annotations

import csv
import sqlite3

import pytest

from winnow import multicase as mc
from winnow.multicase import CaseReader, NotACaseFile
from winnow.store import DEFAULT_TAGS, Store

HEADER = ["Timestamp", "EventId", "Host", "User"]
HOSTS = {
    "hostA": [["2024-01-05 13:22:01", "4624", "HOST-A", "alice"],
              ["2024-01-05 14:00:00", "4625", "HOST-A", "evil.exe"]],
    "hostB": [["2024-01-05 13:30:00", "4624", "HOST-B", "evil.exe"]],
    "hostC": [["2024-01-06 08:00:00", "4624", "HOST-C", "bob"]],
}


@pytest.fixture
def cases(tmp_path):
    """Three closed cases on disk, as an analyst's set of hosts."""
    paths = []
    for host, rows in HOSTS.items():
        csv_path = tmp_path / f"{host}.csv"
        with open(csv_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(HEADER)
            w.writerows(rows)
        db = tmp_path / f"{host}.db"
        st = Store(str(db), default_tags=DEFAULT_TAGS)
        st.ingest_csv(str(csv_path), name=f"{host}.csv", build_fts=False)
        st.close()
        paths.append(str(db))
    return paths


# ------------------------------------------------------------- the reader

def test_a_case_open_elsewhere_is_still_readable(tmp_path):
    """The normal state: another Winnow holds the case, WAL is active. If
    this didn't work the whole approach would be dead on arrival."""
    csv_path = tmp_path / "live.csv"
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(HOSTS["hostA"])
    live = Store(str(tmp_path / "live.db"), default_tags=DEFAULT_TAGS)
    live.ingest_csv(str(csv_path), name="live.csv", build_fts=False)
    try:
        with CaseReader(str(tmp_path / "live.db")) as r:
            assert [s["row_count"] for s in r.sources()] == [2]
            assert r.find_values(["evil.exe"])
    finally:
        live.close()


def test_the_reader_never_writes(cases):
    """Not a promise in a docstring: the connection is mode=ro."""
    with CaseReader(cases[0]) as r:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            r.db.execute("UPDATE sources SET name='hacked'")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            r.db.execute("CREATE TABLE x(y)")


def test_it_takes_no_case_lock(cases, tmp_path):
    """A reader must not make the case look busy to the Winnow that owns
    it — opening it read-write afterwards has to still work."""
    from winnow.store import probe_case_lock
    with CaseReader(cases[0]):
        assert probe_case_lock(cases[0]) is None
    st = Store(cases[0])          # would raise if the reader had claimed it
    st.close()


@pytest.mark.parametrize("content", [b"garbage", b""])
def test_a_file_that_is_not_a_case_is_refused(tmp_path, content):
    p = tmp_path / "notacase.db"
    p.write_bytes(content)
    with pytest.raises(NotACaseFile):
        CaseReader(str(p))


def test_a_sqlite_file_that_is_not_a_winnow_case_is_refused(tmp_path):
    p = tmp_path / "other.db"
    sqlite3.connect(p).execute("CREATE TABLE unrelated (x)")
    with pytest.raises(NotACaseFile, match="no Winnow case tables"):
        CaseReader(str(p))


# ------------------------------------------------------------- A: the sweep

def test_the_sweep_finds_a_value_across_cases(cases):
    out = mc.sweep_values(cases, ["evil.exe"])
    by_case = {c["case"]: c for c in out}
    assert sorted(by_case) == ["hostA", "hostB", "hostC"]
    assert len(by_case["hostA"]["hits"]) == 1
    assert len(by_case["hostB"]["hits"]) == 1
    assert by_case["hostC"]["hits"] == []
    hit = by_case["hostA"]["hits"][0]
    assert hit["rid"] == 2 and hit["source"] == "hostA.csv"
    assert hit["cells"]["User"] == "evil.exe"      # enough context to recognise it


def test_the_sweep_matches_substrings_and_ignores_case_of_the_column(cases):
    assert sum(len(c["hits"]) for c in mc.sweep_values(cases, ["HOST-"])) == 4
    assert sum(len(c["hits"]) for c in mc.sweep_values(cases, ["4625"])) == 1


def test_one_unreadable_case_does_not_sink_the_sweep(cases, tmp_path):
    bad = tmp_path / "broken.db"
    bad.write_bytes(b"not a database")
    out = mc.sweep_values([cases[0], str(bad)], ["evil.exe"])
    assert len(out) == 2
    assert out[0]["error"] is None and out[0]["hits"]
    assert out[1]["error"] and out[1]["hits"] == []


def test_the_sweep_caps_per_case(cases):
    out = mc.sweep_values(cases, ["HOST-"], limit_per_case=1)
    assert all(len(c["hits"]) <= 1 for c in out)
    assert any(c["truncated"] for c in out)


# --------------------------------------------------------- B: cross-case SQL

def test_a_query_can_join_two_cases(cases):
    # c1/c2 are SCHEMA names, so the tables still need their own aliases —
    # the same thing an analyst will trip over, which is why the UI shows
    # the attached schema.
    r = mc.query_across(cases[:2],
                        "SELECT a.User FROM c1.src_1 a JOIN c2.src_1 b USING (User)")
    assert r["rows"] == [["evil.exe"]], "the shared account should join across the two hosts"
    assert [c["alias"] for c in r["cases"]] == ["c1", "c2"]


def test_aliases_follow_the_order_given(cases):
    r = mc.query_across(cases, "SELECT COUNT(*) FROM c3.src_1")
    assert r["rows"] == [[1]] and r["cases"][2]["name"] == "hostC"


@pytest.mark.parametrize("sql", [
    "ATTACH DATABASE '/etc/passwd' AS x",
    "attach database 'x' as y",
    "PRAGMA table_info(sources)",
    "VACUUM",
    "DETACH DATABASE c1",
])
def test_statements_that_would_reach_outside_are_refused(cases, sql):
    with pytest.raises(ValueError, match="aren't allowed"):
        mc.query_across(cases, sql)


def test_a_keyword_inside_a_string_is_not_a_keyword(cases):
    r = mc.query_across(cases[:1], "SELECT 'pragma vacuum' AS s")
    assert r["rows"] == [["pragma vacuum"]]


def test_the_query_cannot_write(cases):
    with pytest.raises(sqlite3.OperationalError):
        mc.query_across(cases[:1], "UPDATE c1.sources SET name='x'")


def test_too_many_cases_is_a_clear_error(cases):
    with pytest.raises(ValueError, match="8 cases or fewer"):
        mc.query_across(cases * 4, "SELECT 1")


def test_the_schema_names_the_aliases(cases):
    text = mc.schema_across(cases[:2])
    assert "attached as c1" in text and "attached as c2" in text
    assert 'CREATE TABLE c1."src_1"' in text and '"Timestamp" TEXT' in text


# ---------------------------------------------------- C: unified timeline

def test_the_timeline_interleaves_cases_in_time_order(cases):
    out = mc.timeline_across(cases)
    assert [r["case"] for r in out["rows"]] == ["hostA", "hostB", "hostA", "hostC"]
    assert [r["when"] for r in out["rows"]] == sorted(r["when"] for r in out["rows"])
    first = out["rows"][0]
    assert first["source"] == "hostA.csv" and first["rid"] == 1
    assert "alice" in first["summary"]


def test_the_timeline_windows_by_time(cases):
    out = mc.timeline_across(cases, start="2024-01-05 13:25:00", end="2024-01-05 14:30:00")
    assert [r["when"] for r in out["rows"]] == ["2024-01-05 13:30:00", "2024-01-05 14:00:00"]


def test_the_timeline_says_when_it_truncated(cases):
    out = mc.timeline_across(cases, limit=2)
    assert len(out["rows"]) == 2 and out["truncated"] and out["total_seen"] == 4


def test_a_source_with_no_timestamp_column_is_skipped(tmp_path, cases):
    csv_path = tmp_path / "notime.csv"
    csv_path.write_text("Host,User\nH9,carol\n")
    st = Store(str(tmp_path / "notime.db"), default_tags=DEFAULT_TAGS)
    st.ingest_csv(str(csv_path), name="notime.csv", build_fts=False)
    st.close()
    out = mc.timeline_across([str(tmp_path / "notime.db")])
    assert out["rows"] == [], "a table with nothing to place in time has no timeline rows"


def test_a_broken_case_is_reported_not_raised(cases, tmp_path):
    bad = tmp_path / "broken.db"
    bad.write_bytes(b"nope")
    out = mc.timeline_across([cases[0], str(bad)])
    assert out["rows"] and len(out["errors"]) == 1 and out["errors"][0]["path"] == str(bad)


# ------------------------------------------------------------------ routes

@pytest.fixture
def registered(cases, monkeypatch):
    """The three cases in the workspace registry, which is what the routes
    are allowed to read."""
    from winnow import workspace as WS
    for p in cases:
        WS.cases.create(p, name=p.split("/")[-1].replace(".db", ""))
    return cases


def test_the_routes_only_read_registered_cases(client, registered, tmp_path):
    """A path from the browser is not a licence to open any file on disk:
    the case list is the analyst's own statement of what they work on."""
    stranger = tmp_path / "stranger.db"
    st = Store(str(stranger), default_tags=DEFAULT_TAGS)
    st.close()
    r = client.post("/api/multicase/sweep",
                    json={"paths": [str(stranger)], "values": ["x"]})
    assert r.status_code == 400 and "registered" in r.json()["detail"]


def test_the_case_list_marks_the_open_one(client, registered, store):
    body = client.get("/api/multicase/cases").json()
    assert body["attach_budget"] >= 2
    paths = {c["path"]: c for c in body["cases"]}
    assert set(registered) <= set(paths)
    assert all(c["exists"] for c in body["cases"] if c["path"] in registered)


def test_sweep_route_defaults_to_this_cases_watchlist(client, registered, store):
    store.add_indicator("evil.exe", "other", None, None)
    out = client.post("/api/multicase/sweep", json={}).json()
    assert out["values"] == ["evil.exe"]
    assert out["total_hits"] == 2                      # hostA and hostB
    by_case = {c["case"]: c for c in out["cases"]}
    assert len(by_case["hostA"]["hits"]) == 1 and by_case["hostC"]["hits"] == []


def test_sweep_route_says_so_when_there_is_nothing_to_look_for(client, registered, store):
    r = client.post("/api/multicase/sweep", json={})
    assert r.status_code == 400 and "watchlist" in r.json()["detail"]


def test_sql_route_runs_across_cases_and_refuses_the_rest(client, registered):
    r = client.post("/api/multicase/sql",
                    json={"paths": registered[:2], "sql": "SELECT COUNT(*) FROM c2.src_1"})
    assert r.status_code == 200 and r.json()["rows"] == [[1]]
    bad = client.post("/api/multicase/sql",
                      json={"paths": registered[:1], "sql": "PRAGMA table_info(sources)"})
    assert bad.status_code == 400
    broken = client.post("/api/multicase/sql",
                         json={"paths": registered[:1], "sql": "SELECT * FROM nope"})
    assert broken.status_code == 400 and "SQL error" in broken.json()["detail"]


def test_schema_route_describes_the_attached_cases(client, registered):
    text = client.post("/api/multicase/schema", json={"paths": registered[:2]}).json()["schema"]
    assert "attached as c1" in text and "attached as c2" in text


def test_timeline_route_interleaves_and_windows(client, registered):
    out = client.post("/api/multicase/timeline", json={"paths": registered}).json()
    assert [r["case"] for r in out["rows"]] == ["hostA", "hostB", "hostA", "hostC"]
    windowed = client.post("/api/multicase/timeline",
                           json={"paths": registered, "start": "2024-01-05 13:25:00",
                                 "end": "2024-01-05 14:30:00"}).json()
    assert len(windowed["rows"]) == 2


def test_the_open_case_is_read_from_its_own_store_not_twice(client, registered, store, monkeypatch):
    """The open case must not be opened a second time read-only — it would
    be a second connection to a file this process is already writing."""
    from winnow import workspace as WS
    WS.cases.create(store.path, name="the-open-one")
    seen = []
    real = mc.CaseReader.__init__

    def spy(self, path, name=None):
        seen.append(path)
        return real(self, path, name)

    monkeypatch.setattr(mc.CaseReader, "__init__", spy)
    client.post("/api/multicase/timeline", json={})
    assert store.path not in seen, "the open case was opened again as a reader"
