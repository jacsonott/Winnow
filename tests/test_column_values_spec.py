"""Store.column_values(spec=...) and POST /api/column_values: a column's
values and counts over the rows a view spec leaves, rather than the whole
table. It is what the header value picker lists under "This view" for a
column that already has picks of its own — the client sends its view's
spec less those picks, which is Excel's rule: every other filter narrows
a column's drop-down, its own picks do not.

The reported bug: filter Host, then open User's drop-down while User
already had a selection, and the list still offered every user with
whole-table counts — tick one Host had ruled out and the grid filtered to
nothing. The picker listed the whole table for any column with picks.

The counts have to be the grid's own, so the main test here holds them to
group_summary over a view built from the same spec, for every kind of
narrowing build_view knows: column filters, the filter tree (raw SQL
included), each search mode, tags, the timeframe, Hide empty rows and
Hide duplicate rows (with the tag filter after it) — on a table and on a
merge (invariant #9), with a derived column on both sides of it.
"""
from __future__ import annotations

import pytest

ROWS = [
    ["When", "Host", "User", "Event"],
    ["2024-01-01 09:00:00", "WS01", "alice", "4624"],   # 1
    ["2024-01-01 09:00:00", "WS01", "alice", "4624"],   # 2: 1 again
    ["2024-01-01 10:00:00", "WS01", "bob", "4625"],     # 3
    ["2024-01-02 11:00:00", "WS02", "carol", "4624"],   # 4
    ["2024-01-02 12:00:00", "WS02", "dave", "4688"],    # 5
]
ROWS_B = [
    ["When", "Host", "User", "Event"],
    ["2024-01-03 08:00:00", "DC01", "alice", "4672"],   # 1
    ["2024-01-03 08:30:00", "DC01", "admin", "4624"],   # 2
    ["", "", "", ""],                                   # 3: an empty row
    ["2024-01-04 09:00:00", "WS01", "", "4624"],        # 4
    ["2024-01-01 09:00:00", "WS01", "alice", "4624"],   # 5: a.csv's row 1, in the other member
]
ALL = ROWS[0]


def _spec(sid, **extra):
    return {"source_id": sid, "filters": [], "sort": [], **extra}


def _listed(store, sid, column, **extra):
    return {r["value"]: r["count"] for r in store.column_values(sid, column, limit=1000, spec=_spec(sid, **extra))}


def _via_view(store, sid, column, **extra):
    v = store.build_view(sid, _spec(sid, **extra))
    res = store.group_summary(v["view_id"], column, limit=1000, bucket_datetime=False)
    return {g["value"]: g["count"] for g in res["groups"]}


def _ingest(store, write_csv, rows, name):
    return store.ingest_csv(write_csv(rows, name), name=name.split(".")[0], build_fts=False)["id"]


def _tag(store, name):
    return store.upsert_tag(None, name, "#c0392b", None)["id"]


@pytest.fixture
def table(store, write_csv):
    """One table holding both files' rows, tagged: rows 1 and 6 Seen, row
    3 Odd. Returns (source id, tag ids)."""
    sid = _ingest(store, write_csv, ROWS + ROWS_B[1:], "both.csv")
    seen, odd = _tag(store, "Seen"), _tag(store, "Odd")
    store.set_tags(sid, [1, 6], seen, True)
    store.set_tags(sid, [3], odd, True)
    return sid, {"seen": seen, "odd": odd}


@pytest.fixture
def merged(store, write_csv):
    """The same rows as a merge of the two files, tagged on the members
    (a merge's tags live there): a.csv row 1 and b.csv row 1 Seen, a.csv
    row 3 Odd. Returns (merge id, tag ids)."""
    a = _ingest(store, write_csv, ROWS, "a.csv")
    b = _ingest(store, write_csv, ROWS_B, "b.csv")
    mid = store.create_merge("both", [a, b])["id"]
    seen, odd = _tag(store, "Seen"), _tag(store, "Odd")
    store.set_tags(a, [1], seen, True)
    store.set_tags(b, [1], seen, True)
    store.set_tags(a, [3], odd, True)
    return mid, {"seen": seen, "odd": odd}


def _host_in(*hosts):
    return {"type": "group", "op": "AND",
            "children": [{"type": "cond", "column": "Host", "op": "in", "value": list(hosts)}]}


# Each a spec with one kind of narrowing (`tags` names tag keys, resolved
# per fixture), and the column whose list is read under it.
NARROWINGS = {
    "column filter": ("User", {"filters": [{"column": "Host", "op": "equals", "value": "WS01"}]}),
    "a filter on the column itself": ("User", {"filters": [{"column": "User", "op": "contains", "value": "a"}]}),
    "filter tree": ("User", {"filter_tree": _host_in("WS01", "DC01")}),
    "raw SQL in the tree": ("Host", {"filter_tree": {"type": "raw", "sql": "\"Event\" = '4624'"}}),
    "search, contains": ("User", {"search": "ws0"}),
    "search, regex": ("User", {"search": "^2024-01-0[12]", "search_mode": "regex"}),
    "search, advanced": ("Host", {"search_mode": "advanced",
                                  "search_terms": [{"term": "alice", "connector": "AND", "exclude": False},
                                                   {"term": "DC01", "connector": "AND", "exclude": True}]}),
    "tags, any of": ("User", {"tags": ["seen"]}),
    "tags, untagged": ("Host", {"tags": ["__none__"]}),
    "tags, excluded": ("User", {"tags": ["!odd"]}),
    "timeframe": ("Host", {"time_range": {"enabled": True, "column": "", "start": "2024-01-02 00:00:00",
                                          "end": "2024-01-03 23:59:59"}}),
    "hide empty rows": ("User", {"hide_empty_rows": True}),
    "hide duplicate rows": ("User", {"dedupe_columns": ALL}),
    "hide duplicates, then a tag filter": ("User", {"dedupe_columns": ALL, "tags": ["__none__"]}),
    "hide duplicates under a filter": ("Event", {"dedupe_columns": ["When", "Host", "User"],
                                                 "filters": [{"column": "Host", "op": "equals", "value": "WS01"}]}),
    "everything at once": ("User", {"filters": [{"column": "Event", "op": "equals", "value": "4624"}],
                                    "search": "0", "tags": ["!odd"], "hide_empty_rows": True,
                                    "dedupe_columns": ALL}),
}


def _resolved(extra, tags):
    if "tags" not in extra:
        return extra
    names = {"seen": tags["seen"], "odd": tags["odd"], "!odd": f"!{tags['odd']}"}
    return {**extra, "tags": [names.get(t, t) for t in extra["tags"]]}


@pytest.mark.parametrize("kind", NARROWINGS)
@pytest.mark.parametrize("shape", ["table", "merged"])
def test_the_counts_are_the_grids_for_every_kind_of_narrowing(request, store, kind, shape):
    sid, tags = request.getfixturevalue(shape)
    column, extra = NARROWINGS[kind]
    extra = _resolved(extra, tags)
    listed = _listed(store, sid, column, **extra)
    assert listed == _via_view(store, sid, column, **extra)
    # And it did narrow: a spec this test thinks filters something but
    # the store ignores would pass the line above as the whole table.
    whole = {r["value"]: r["count"] for r in store.column_values(sid, column, limit=1000)}
    assert listed != whole


@pytest.mark.parametrize("shape", ["table", "merged"])
def test_the_reported_case(request, store, shape):
    """Host filtered to WS01: User lists only WS01's users, WS01's counts."""
    sid, _ = request.getfixturevalue(shape)
    on_ws01 = {"filters": [{"column": "Host", "op": "equals", "value": "WS01"}]}
    assert _listed(store, sid, "User", **on_ws01) == {"alice": 3, "bob": 1, "": 1}
    # With duplicates hidden, the two copies of alice's 09:00 logon in
    # a.csv, and the third in b.csv, count once.
    assert _listed(store, sid, "User", dedupe_columns=ALL, **on_ws01) == {"alice": 1, "bob": 1, "": 1}


def test_a_derived_column_on_both_sides(store, write_csv):
    """Listed as the picked column, and filtered on as another one."""
    sid = _ingest(store, write_csv, ROWS + ROWS_B[1:], "both.csv")
    res = store.add_derived_column(sid, "Initial", "User", "regex_extract", {"pattern": r"^(\w)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    on_ws01 = {"filters": [{"column": "Host", "op": "equals", "value": "WS01"}]}
    assert _listed(store, sid, "Initial", **on_ws01) == _via_view(store, sid, "Initial", **on_ws01)
    on_a = {"filters": [{"column": "Initial", "op": "equals", "value": "a"}]}
    assert _listed(store, sid, "Host", **on_a) == {"WS01": 3, "DC01": 2} == _via_view(store, sid, "Host", **on_a)


@pytest.mark.parametrize("shape", ["table", "merged"])
def test_a_spec_that_narrows_nothing_is_the_whole_table(request, store, shape):
    sid, _ = request.getfixturevalue(shape)
    assert store.column_values(sid, "User", spec=_spec(sid)) == store.column_values(sid, "User")
    assert store._column_values_in_spec(sid, "User", _spec(sid), 200) is None
    # A sort alone is no narrowing either.
    by_user = _spec(sid, sort=[{"column": "User", "dir": "desc"}])
    assert store._column_values_in_spec(sid, "User", by_user, 200) is None


def test_most_common_first_and_capped(table, store):
    sid, _ = table
    on_ws01 = _spec(sid, filters=[{"column": "Host", "op": "equals", "value": "WS01"}])
    assert store.column_values(sid, "User", limit=1, spec=on_ws01) == [{"value": "alice", "count": 3}]


@pytest.mark.parametrize("shape", ["table", "merged"])
def test_the_grids_view_is_left_alone(request, store, shape):
    """An aggregate, not a second view: build_view keeps one live view per
    source, so building one for the list would evict the grid's — or the
    search held in the background."""
    sid, _ = request.getfixturevalue(shape)
    live = store.build_view(sid, _spec(sid, filters=[{"column": "User", "op": "equals", "value": "alice"}]))
    held = store.build_view(sid, _spec(sid, search="WS0"), hold=True)
    before = dict(store._views)
    _listed(store, sid, "User", filters=[{"column": "Host", "op": "equals", "value": "WS01"}], dedupe_columns=ALL)
    assert store._views == before
    assert store.fetch_rows(live["view_id"], 0, 10)["rows"]
    assert store.adopt_view(held["view_id"])["view_id"] == held["view_id"]


def test_an_unknown_column_is_a_key_error(table, store):
    sid, _ = table
    with pytest.raises(KeyError):
        store.column_values(sid, "Nope", spec=_spec(sid, search="x"))


def test_the_route(client, table):
    sid, _ = table
    body = {"source_id": sid, "column": "User", "limit": 10,
            "spec": _spec(sid, filters=[{"column": "Host", "op": "equals", "value": "WS02"}])}
    r = client.post("/api/column_values", json=body)
    assert r.status_code == 200, r.text
    assert {x["value"]: x["count"] for x in r.json()} == {"carol": 1, "dave": 1}
    # No spec at all is the GET's whole-table list.
    r = client.post("/api/column_values", json={"source_id": sid, "column": "Host"})
    assert r.json() == client.get(f"/api/column_values?source_id={sid}&column=Host").json()


def test_the_route_refuses_what_the_analyst_can_fix(client, table):
    sid, _ = table
    bad = client.post("/api/column_values", json={
        "source_id": sid, "column": "User", "spec": _spec(sid, search="(", search_mode="regex")})
    assert bad.status_code == 400
    missing = client.post("/api/column_values", json={
        "source_id": sid, "column": "Nope", "spec": _spec(sid, search="x")})
    assert missing.status_code == 404


def test_equal_counts_list_in_the_same_order_before_and_after_the_index(store, write_csv):
    """Ties by value. Without it, equal counts came out in the GROUP BY's
    order, which the column index changes when it finishes building in the
    background: WS02 and DC01 (2 each) swapped between two opens of the
    same list, and test_the_route failed whenever its two calls straddled
    the build."""
    sid = _ingest(store, write_csv, ROWS + ROWS_B[1:], "both.csv")
    store._ensure_column_index_building = lambda *a, **k: None   # no index yet
    before = store.column_values(sid, "Host")
    del store._ensure_column_index_building
    store.column_values(sid, "Host")
    store.wait_for_column_index(sid, "Host", timeout=30)
    after = store.column_values(sid, "Host")
    assert before == after
    assert [r["value"] for r in after] == ["WS01", "DC01", "WS02", ""]
