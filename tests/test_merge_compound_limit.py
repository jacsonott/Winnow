"""Merges past SQLite's compound SELECT limit.

Every read of a merge is a UNION ALL with a branch per member, and SQLite
refuses a compound of more than 500 terms ("too many terms in compound
SELECT"). A case that merged 1000 IIS logs could not even open its merged
view: POST /api/view answered 500. store.union_all nests the branches once
there are more than MAX_COMPOUND_TERMS of them; these tests drive every
path that unions a merge's members, over a merge of real W3C logs just
past the limit, and First/Last past the reference limit a merge that size
used to hit as well."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest

from winnow import store as store_mod
from winnow.plugin_api import PluginRegistry
from winnow.store import DEFAULT_TAGS, MAX_COMPOUND_TERMS, TAG_GROUP_COLUMN, Store, union_all

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"

# Just past the limit: a plain union of these many branches is refused.
N_MEMBERS = MAX_COMPOUND_TERMS + 10
TS = "Timestamp (UTC)"
# Every field the "IIS web logs (W3C)" header set names, so the dashboards'
# {{all:…}} placeholder matches these tables too.
FIELDS = ("date time s-ip cs-method cs-uri-stem cs-uri-query s-port cs-username c-ip "
          "cs(User-Agent) cs(Referer) sc-status sc-substatus sc-win32-status time-taken")


def _limited(conn):
    """The stock ceiling, whatever this SQLite was built with (setlimit
    can only lower it, and needs Python 3.11)."""
    if hasattr(conn, "setlimit"):
        conn.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, MAX_COMPOUND_TERMS)
    return conn


# ------------------------------------------------------------- union_all


def test_union_all_is_the_plain_join_up_to_the_cap():
    branches = [f"SELECT {i} AS n" for i in range(MAX_COMPOUND_TERMS)]
    assert union_all(branches) == " UNION ALL ".join(branches)
    assert union_all(branches, "\nUNION ALL\n") == "\nUNION ALL\n".join(branches)


@pytest.mark.parametrize("n", [MAX_COMPOUND_TERMS + 1, 2 * MAX_COMPOUND_TERMS + 1])
def test_union_all_runs_where_the_plain_join_is_refused(n):
    branches = [f"SELECT {i} AS n" for i in range(n)]
    conn = _limited(sqlite3.connect(":memory:"))
    with pytest.raises(sqlite3.OperationalError, match="too many terms in compound SELECT"):
        conn.execute(" UNION ALL ".join(branches))
    assert [r[0] for r in conn.execute(union_all(branches))] == list(range(n))


def test_union_all_keeps_branch_and_parameter_order_at_every_level(monkeypatch):
    """A cap of 2 nests 11 branches three levels deep (11 -> 6 -> 3 -> 2).
    Each branch binds its own `?`, so any reordering shows up as a row
    pairing the wrong i with the wrong parameter."""
    monkeypatch.setattr(store_mod, "MAX_COMPOUND_TERMS", 2)
    n = 11
    sql = union_all(f"SELECT {i} AS i, ? AS p" for i in range(n))
    assert sql.count("SELECT * FROM (") == 6 + 3 + 2
    rows = sqlite3.connect(":memory:").execute(sql, [f"p{i}" for i in range(n)]).fetchall()
    assert rows == [(i, f"p{i}") for i in range(n)]


# --------------------------------------------- a merge of 510 IIS logs


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    """One case, N_MEMBERS daily W3C logs (u_exYYMMDD.log, two requests
    each), merged. Module-scoped: importing 510 files once is what keeps
    this file quick.

    The background index/FTS builds that filters, sorts and searches kick
    off are switched off, as bench/fixtures.py's _quiet_background does:
    each one commits a schema change per member, which is beside the
    point here and would have every read below racing 510 of them."""
    tmp = tmp_path_factory.mktemp("big_merge")
    s = Store(str(tmp / "case.db"), default_tags=DEFAULT_TAGS)
    s._ensure_fts_building = lambda *a, **k: None
    s._ensure_column_index_building = lambda *a, **k: None
    s._ensure_sort_index_building = lambda *a, **k: None
    ids = []
    for i in range(N_MEMBERS):
        day = datetime.date(2024, 1, 1) + datetime.timedelta(days=i)
        path = tmp / f"u_ex{day:%y%m%d}.log"
        path.write_text(
            "#Software: Microsoft Internet Information Services 10.0\n"
            f"#Fields: {FIELDS}\n"
            f"{day} 08:00:00 10.0.0.5 GET /login.aspx - 443 - 203.0.113.{i % 250} "
            f"Mozilla/5.0 - 200 0 0 {i}\n"
            f"{day} 09:00:00 10.0.0.5 POST /upload{i}.aspx - 443 admin 198.51.100.7 "
            f"curl/8.0 - 500 0 0 {i}\n", encoding="utf-8")
        ids.append(s.ingest_iis(str(path), build_fts=False)["id"])
    mid = s.create_merge("all W3SVC1 logs", ids)["id"]
    yield s, ids, mid
    s.close()


def _spec(mid, **kw):
    return {"source_id": mid, "filters": [], "sort": [], **kw}


GET = [{"column": "cs-method", "op": "equals", "value": "GET"}]


def test_merged_view_builds_in_order_past_the_limit(big):
    s, ids, mid = big
    for spec, n in ((_spec(mid), 2 * N_MEMBERS),
                    (_spec(mid, filters=GET), N_MEMBERS),
                    (_spec(mid, sort=[{"column": "cs-uri-stem", "dir": "asc"}]), 2 * N_MEMBERS),
                    (_spec(mid, filters=GET, sort=[{"column": "time-taken", "dir": "desc"}]), N_MEMBERS),
                    (_spec(mid, search=f"upload{N_MEMBERS - 1}."), 1)):
        assert s.build_view(mid, spec)["row_count"] == n, spec

    # Newest first across every member, the ones past #500 included.
    v = s.build_view(mid, _spec(mid, sort=[{"column": TS, "dir": "desc"}]))
    rows = s.fetch_rows(v["view_id"], 0, 2 * N_MEMBERS)["rows"]
    assert [(r["source_id"], r["rid"]) for r in rows] == [
        (sid, rid) for sid in reversed(ids) for rid in (2, 1)]


def test_a_sorted_merge_past_the_limit_still_merges_its_sort_indexes(big):
    """The nesting is undone by SQLite's flattener: the unfiltered sort
    still streams every member through its sort index (built at import
    for the first datetime column) and merges them, rather than sorting
    the lot in a temp B-tree — the shape test_merge_view_build pins for
    small merges."""
    s, ids, mid = big
    seen = []
    s.db.set_trace_callback(seen.append)
    try:
        s.build_view(mid, _spec(mid, sort=[{"column": TS, "dir": "asc"}]))
    finally:
        s.db.set_trace_callback(None)
    insert = next(x for x in reversed(seen) if x.startswith("INSERT INTO v."))
    sql = insert[insert.index("SELECT"):]
    assert "SELECT * FROM (" in sql    # nested, or this proves nothing
    plan = [r["detail"] for r in s.db.execute("EXPLAIN QUERY PLAN " + sql)]
    reads = [d for d in plan if d.startswith(("SCAN src_", "SEARCH src_"))]
    assert len(reads) == len(ids)
    assert all("COVERING INDEX" in d for d in reads), reads[:3]
    assert not any("TEMP B-TREE" in d for d in plan)


def test_opening_a_big_merge_reads_its_columns_once(big, monkeypatch):
    """get_source on a merge worked its column list out again for every
    member's "missing columns", which read every member's columns each
    time: quadratic, ~3 s per open of this merge and four times that at
    1000 members, on every route that resolves one."""
    s, ids, mid = big
    calls = []
    real = Store._merge_columns
    monkeypatch.setattr(Store, "_merge_columns",
                        staticmethod(lambda row, members: calls.append(1) or real(row, members)))
    src = s.get_source(mid)
    assert len(calls) == 1
    assert [m["missing"] for m in src["members"]] == [[]] * len(ids)


def test_view_route_past_the_limit(big, monkeypatch):
    """The request in the original report."""
    import server
    from fastapi.testclient import TestClient

    s, _, mid = big
    monkeypatch.setattr(server, "STORE", s)
    client = TestClient(server.app, headers={"X-Timeline-Lite-Client": "1"})
    r = client.post("/api/view", json=_spec(mid))
    assert r.status_code == 200, r.text
    assert r.json()["row_count"] == 2 * N_MEMBERS


@pytest.mark.parametrize("filtered", [False, True])
def test_aggregates_over_the_view_past_the_limit(big, filtered):
    """Both shapes: an unfiltered view reads the member tables directly, a
    filtered one joins the materialised view — each its own union."""
    s, _, mid = big
    vid = s.build_view(mid, _spec(mid, filters=GET) if filtered else _spec(mid))["view_id"]
    rows = N_MEMBERS if filtered else 2 * N_MEMBERS

    assert sum(g["count"] for g in s.group_summary(vid, "cs-method")["groups"]) == rows
    assert s.group_summary(vid, TAG_GROUP_COLUMN)["groups"] == [{"value": None, "count": rows}]
    assert s.distinct_values(vid, "cs-method")["values"] == (["GET"] if filtered else ["GET", "POST"])
    assert s.expand_group(vid, "cs-method", "GET")["row_count"] == N_MEMBERS
    assert s.time_histogram(vid, TS)["total"] == rows
    assert s.time_histogram(vid, TS, stack="tags")["total"] == rows
    split = s.time_histogram(vid, TS, stack="values", split_column="cs-method", split_values=["GET"])
    assert split["total"] == rows
    assert len(s._sample_column_in_view(vid, "cs-uri-stem")) > 0


def test_column_values_past_the_limit(big):
    s, _, mid = big
    assert {v["value"]: v["count"] for v in s.column_values(mid, "cs-method")} == {
        "GET": N_MEMBERS, "POST": N_MEMBERS}


def test_sql_pane_and_dashboards_past_the_limit(big):
    s, _, mid = big
    # The pane's merge_<id> view used to be skipped (its CREATE failed and
    # is caught), leaving the analyst a "no such table".
    assert s.run_sql(f"SELECT count(*) FROM merge_{-mid}")["rows"] == [[2 * N_MEMBERS]]
    # "Open this filter in the SQL pane" on the merge.
    sql = s.spec_sql(mid, _spec(mid, filters=GET, sort=[{"column": "cs-uri-stem", "dir": "asc"}]))
    assert len(s.run_sql(sql)["rows"]) == N_MEMBERS
    # A dashboard widget over every table of a header set.
    sql = s._resolve_table_placeholders("SELECT count(*) FROM {{all:header_set:IIS web logs (W3C)}}")
    assert s.run_sql(sql)["rows"] == [[2 * N_MEMBERS]]


def test_tagged_rows_past_the_limit(big):
    """The Timeline and the watchlist's latest hits union a branch per
    source holding a finding."""
    s, ids, mid = big
    tag = s.list_tags()[0]["id"]
    s.tag_view(s.build_view(mid, _spec(mid, filters=GET))["view_id"], tag, True)
    try:
        assert s.build_timeline()["row_count"] == N_MEMBERS
        root = s.build_view(mid, _spec(mid))["view_id"]
        counts = {g["value"]: g["count"] for g in s.group_summary(root, TAG_GROUP_COLUMN)["groups"]}
        assert counts == {tag: N_MEMBERS, None: N_MEMBERS}
    finally:
        s.tag_view(s.build_view(mid, _spec(mid, filters=GET))["view_id"], tag, False)

    # The hits a scan for the POST rows' client would record, written
    # directly: scan_all finds the same ones, but reading all 510 tables to
    # do it would be most of this file's run time.
    wid = s.add_indicator("198.51.100.7", kind="ip")["id"]
    try:
        with s.lock, s.db:
            s.db.executemany("INSERT INTO watchlist_hits(watchlist_id, source_id, rid) VALUES (?, ?, 2)",
                             [(wid, sid) for sid in ids])
        assert len(s.latest_hits(limit=2 * N_MEMBERS)["rows"]) == N_MEMBERS
    finally:
        s.delete_indicator(wid)


def test_plugins_past_the_limit(big, monkeypatch):
    """pivot, first_last and lateral_movement each read a merge through
    their own union of its members (_scope)."""
    import server
    from fastapi.testclient import TestClient

    s, _, mid = big
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    monkeypatch.setattr(server, "STORE", s)
    monkeypatch.setattr(server, "PLUGINS", reg)
    client = TestClient(server.app, headers={"X-Timeline-Lite-Client": "1"})

    def post(route, body):
        r = client.post(route, json=body)
        assert r.status_code == 200, r.text
        return r.json()

    pivot = post("/api/plugin/pivot/aggregate", {"source_id": mid, "group_sets": [["cs-method"]]})
    assert sorted(pivot["sets"][0]["rows"]) == [["GET", N_MEMBERS], ["POST", N_MEMBERS]]
    values = post("/api/plugin/first_last/values", {"source_id": mid, "column": "cs-method"})["values"]
    assert {v["value"]: v["count"] for v in values} == {"GET": N_MEMBERS, "POST": N_MEMBERS}
    edges = post("/api/plugin/lateral_movement/edges", {"selections": [
        {"source_id": mid, "src_col": "c-ip", "dst_col": "s-ip"}]})["edges"]
    assert sum(e["n"] for e in edges) == 2 * N_MEMBERS


def test_first_last_bookends_past_the_reference_limit(big, monkeypatch):
    """first_last looked each bookend up with an arm per member that joined
    its CTE of bookends, and SQLite expands a CTE once per reference: every
    arm carried the whole union of members again, members squared
    references to each table. Past 65,535 of them ("too many references
    to row_tags", from about 200 members) the preview and Copy result
    answered 500."""
    import server
    from fastapi.testclient import TestClient

    s, _, mid = big
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    monkeypatch.setattr(server, "STORE", s)
    monkeypatch.setattr(server, "PLUGINS", reg)
    client = TestClient(server.app, headers={"X-Timeline-Lite-Client": "1"})
    body = {"source_id": mid, "group_by": ["c-ip"], "sort_column": TS,
            "columns": ["c-ip", "cs-uri-stem"], "sum_columns": ["time-taken"]}

    def post(route):
        r = client.post(f"/api/plugin/first_last/{route}", json=body)
        assert r.status_code == 200, r.text
        out = r.json()
        return [dict(zip(out["columns"], row)) for row in out["rows"]], out

    def day(i, hour):
        return f"{datetime.date(2024, 1, 1) + datetime.timedelta(days=i)} {hour}:00:00"

    # 203.0.113.k requested on days k, k+250 (and k+500 for k < 10);
    # 198.51.100.7 uploaded every day.
    expected = {"198.51.100.7": (day(0, "09"), "/upload0.aspx", day(N_MEMBERS - 1, "09"),
                                 f"/upload{N_MEMBERS - 1}.aspx", N_MEMBERS, sum(range(N_MEMBERS)))}
    for k in range(250):
        days = list(range(k, N_MEMBERS, 250))
        expected[f"203.0.113.{k}"] = (day(days[0], "08"), "/login.aspx", day(days[-1], "08"),
                                      "/login.aspx", len(days), sum(days))
    rows, _ = post("rows")
    assert len(rows) == 2 * len(expected)
    got = {}
    for r in rows:
        which, n = r["Description"].split(" of ")
        got.setdefault(r["c-ip"], {})[which] = (r[TS], r["cs-uri-stem"], int(n), int(r["Sum of time-taken"]))
    assert got == {ip: {"First": (f_ts, f_uri, n, total), "Last": (l_ts, l_uri, n, total)}
                   for ip, (f_ts, f_uri, l_ts, l_uri, n, total) in expected.items()}

    preview, out = post("preview")
    assert out["total_groups"] == len(expected)
    assert [r["c-ip"] for r in preview[:4]] == ["203.0.113.0", "198.51.100.7", "203.0.113.1", "203.0.113.2"]
