"""Invariant #9 gap burn-down: raw SQL filter fragments and batch derived
adds on merged tables — both previously refused outright."""

from __future__ import annotations

import pytest

A_ROWS = [["When", "Msg"], ["2024-01-01 10:00", "alpha beacon"], ["2024-01-01 11:00", "beta"]]
B_ROWS = [["When", "Msg"], ["2024-01-02 10:00", "gamma beacon"], ["2024-01-02 11:00", "delta"]]


def _mk(store, write_csv):
    a = store.ingest_csv(write_csv(A_ROWS, "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(B_ROWS, "b.csv"), name="b", build_fts=False)["id"]
    return a, b, store.create_merge("both", [a, b])["id"]


# ------------------------------------------------------------- raw SQL

def test_raw_sql_fragment_filters_a_merge(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": [],
                               "filter_tree": {"type": "raw", "sql": "\"Msg\" LIKE '%beacon%'"}})
    assert v["row_count"] == 2  # one from each member


def test_raw_sql_fragment_reaches_a_merge_derived_column(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    res = store.add_derived_column(mid, "Word", "Msg", "regex_extract", {"pattern": r"^(\w+)"})
    for jid in res["job_ids"]:
        store.wait_for_ingest_job(jid, timeout=30)
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": [],
                               "filter_tree": {"type": "raw", "sql": "\"Word\" = 'beta'"}})
    assert v["row_count"] == 1


def test_merge_raw_sql_validates_and_rejects_like_a_table(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    store.validate_where_fragment(mid, "\"Msg\" LIKE '%a%'")  # must not raise
    # Unknown identifiers hit the allowlist, same as on a plain table —
    # quoted ones too, rather than falling into SQLite's string-literal
    # fallback on both.
    with pytest.raises(ValueError, match="Unknown identifier"):
        store.validate_where_fragment(mid, "Nope = 1")
    with pytest.raises(ValueError, match='"Nope"'):
        store.validate_where_fragment(mid, "\"Nope\" = 1")
    with pytest.raises(ValueError):
        store.validate_where_fragment(mid, "1; DROP TABLE x")


def test_rid_filters_reach_every_member_of_a_merge(store, write_csv):
    """`rid` is legal under the USING(rid) join shape, as a raw fragment
    and as the condition node the session diff pivots with."""
    _, _, mid = _mk(store, write_csv)
    store.validate_where_fragment(mid, "rid IN (1)")
    for tree in ({"type": "raw", "sql": "rid IN (1)"},
                 {"type": "cond", "column": "rid", "op": "in", "value": ["1"]}):
        v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": [], "filter_tree": tree})
        assert v["row_count"] == 2, tree   # rid 1 of each member


def test_merge_spec_sql_carries_the_raw_fragment(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    sql = store.spec_sql(mid, {"source_id": mid, "filters": [], "sort": [],
                               "filter_tree": {"type": "raw", "sql": "\"Msg\" LIKE '%beacon%'"}})
    assert "beacon" in sql
    res = store.run_sql(sql)
    assert len(res["rows"]) == 2


def test_member_only_derived_stays_invisible_to_merge_raw_sql(store, write_csv):
    a, _, mid = _mk(store, write_csv)
    res = store.add_derived_column(a, "OnlyA", "Msg", "regex_extract", {"pattern": "(a)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    # not exposed on the merge (partial coverage) — the identifier check
    # runs against the merge's exposed columns, so a bare reference to one
    # member's private derived column is rejected up front.
    with pytest.raises(ValueError, match="Unknown identifier"):
        store.validate_where_fragment(mid, "OnlyA = 'a'")



def test_members_differing_only_in_non_ascii_case_merge_as_two_columns(store, write_csv):
    """SQLite folds identifier case for ASCII only, so to it "Größe" and
    "GRÖßE" are two columns. The merge rule once folded with str.lower and
    quoted the first member's name against the second, which SQLite's
    double-quoted-string fallback turned into the literal — the second
    member's rows showed the header names as data. Now that tables with
    different columns merge, they are two columns, each empty on the
    member without it."""
    a = store.ingest_csv(write_csv([["Größe", "Öffnen"], ["1", "x"]], "a.csv"), build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["GRÖßE", "öffnen"], ["2", "y"]], "b.csv"), build_fts=False)["id"]
    m = store.create_merge("m", [a, b])
    assert [c["name"] for c in m["columns"]] == ["Größe", "Öffnen", "GRÖßE", "öffnen"]
    v = store.build_view(m["id"], {"source_id": m["id"], "sort": []})
    cells = [r["cells"] for r in store.fetch_rows(v["view_id"], 0, 10)["rows"]]
    assert sorted(cells, key=repr) == sorted([["1", "x", None, None], [None, None, "2", "y"]], key=repr)
    # ASCII case is one column to SQLite, so those are one column of the
    # merge, and every member reads its own values.
    c = store.ingest_csv(write_csv([["SIZE", "open"], ["3", "z"]], "c.csv"), build_fts=False)["id"]
    d = store.ingest_csv(write_csv([["size", "OPEN"], ["4", "w"]], "d.csv"), build_fts=False)["id"]
    mid = store.create_merge("ascii", [c, d])["id"]
    v = store.build_view(mid, {"source_id": mid, "sort": []})
    cells = [r["cells"] for r in store.fetch_rows(v["view_id"], 0, 10)["rows"]]
    assert sorted(cells) == [["3", "z"], ["4", "w"]]
    assert sorted(x["value"] for x in store.column_values(mid, "SIZE")) == ["3", "4"]


def test_a_merge_saved_under_the_old_rule_reads_right(store, write_csv):
    """A case file can already hold a merge the old str.lower rule allowed.
    It opens as two columns rather than paging header names as cell
    values (or, as it briefly did, refusing to open)."""
    a = store.ingest_csv(write_csv([["Größe"], ["1"]], "a.csv"), build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["GRÖßE"], ["2"]], "b.csv"), build_fts=False)["id"]
    with store.lock, store.db:
        store.db.execute("INSERT INTO merges(name, source_ids, created_at) VALUES ('old', ?, '')",
                         (f"[{a}, {b}]",))
    mid = -store.db.execute("SELECT id FROM merges WHERE name='old'").fetchone()[0]
    v = store.build_view(mid, {"source_id": mid, "sort": []})
    cells = [r["cells"] for r in store.fetch_rows(v["view_id"], 0, 10)["rows"]]
    assert sorted(cells, key=repr) == sorted([["1", None], [None, "2"]], key=repr)
    [entry] = [m for m in store.list_merges() if m["id"] == mid]
    assert "error" not in entry and [c["name"] for c in entry["columns"]] == ["Größe", "GRÖßE"]



def _case_split_merge(store, write_csv):
    """Two members whose datetime column differs only in ASCII case — one
    column to SQLite, and to create_merge."""
    a = store.ingest_csv(write_csv([["Time", "Msg"], ["2024-01-01 00:00:00", "a1"],
                                    ["2024-01-02 00:00:00", "a2"]], "a.csv"), build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["TIME", "Msg"], ["2024-06-01 00:00:00", "b1"],
                                    ["2024-06-02 00:00:00", "b2"]], "b.csv"), build_fts=False)["id"]
    return a, b, store.create_merge("m", [a, b])["id"]


def test_jump_to_timestamp_reaches_a_member_spelling_the_column_differently(store, write_csv):
    """find_nearest_timestamp matched member columns case-sensitively, so
    the second member's "TIME" was skipped: jumping to June landed on a
    January row of the first member."""
    a, b, mid = _case_split_merge(store, write_csv)
    for spec in ({"sort": [{"column": "Time", "dir": "asc"}]}, {"sort": []}):
        vid = store.build_view(mid, {"source_id": mid, **spec})["view_id"]
        for column in ("Time", None):
            near = store.find_nearest_timestamp(vid, "2024-06-01 12:00:00", column)
            assert (near["source_id"], near["ts"][:10]) in ((b, "2024-06-01"), (b, "2024-06-02")), near


def test_tag_time_bounds_of_a_merge_reach_a_member_spelling_the_column_differently(store, write_csv):
    """"Set the timeframe from my findings" on a merge skipped the member
    spelling the column "TIME", and answered no range at all."""
    a, b, mid = _case_split_merge(store, write_csv)
    tid = store.list_tags()[0]["id"]
    store.set_tags(b, [1, 2], tid, True)
    bounds = store.tag_time_bounds(mid)
    assert bounds["start"][:10] == "2024-06-01" and bounds["end"][:10] == "2024-06-02"


def _join_index_builds(store):
    with store._threads_lock:
        threads = list(store._index_threads.values())
    for t in threads:
        t.join(30)


def test_a_merges_auto_index_is_listed_and_dropped_on_every_member(store, write_csv):
    """A filter on a merge builds an index on each member, named from the
    column's spelling. It used the merge's spelling ("Host") on a member
    whose column is "HOST", so the Tables modal — which looks indexes up by
    the member's own names — never listed it and could not drop it."""
    a = store.ingest_csv(write_csv([["Host"], ["h1"], ["h2"]], "a.csv"), build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["HOST"], ["h1"], ["h3"]], "b.csv"), build_fts=False)["id"]
    mid = store.create_merge("m", [a, b])["id"]
    store.build_view(mid, {"source_id": mid, "sort": [{"column": "Host", "dir": "asc"}],
                           "filters": [{"column": "Host", "op": "equals", "value": "h1"}]})
    _join_index_builds(store)
    table = store.get_source(b)["table_name"]

    def indexes():
        return {r[0] for r in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,))}

    assert indexes(), "the merge filter should have indexed the member"
    assert [ix["column"] for ix in store.list_column_indexes(b)] == ["HOST"]
    store.drop_column_index(b, "HOST")
    assert indexes() == set()


def test_a_merge_sorts_on_a_data_column_named_source_id(store, write_csv):
    """The merge's UNION projects each row's member id as `source_id`, and
    ORDER BY "source_id" resolved to that rather than to the file's own
    column of the same name: the merge sorted by member."""
    a = store.ingest_csv(write_csv([["source_id", "Msg"], ["zzz", "a1"], ["aaa", "a2"]], "a.csv"),
                         build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["source_id", "Msg"], ["mmm", "b1"], ["bbb", "b2"]], "b.csv"),
                         build_fts=False)["id"]
    mid = store.create_merge("m", [a, b])["id"]
    for direction, expect in (("asc", ["aaa", "bbb", "mmm", "zzz"]), ("desc", ["zzz", "mmm", "bbb", "aaa"])):
        spec = {"source_id": mid, "sort": [{"column": "source_id", "dir": direction}]}
        vid = store.build_view(mid, spec)["view_id"]
        rows = store.fetch_rows(vid, 0, 10)["rows"]
        assert [r["cells"][0] for r in rows] == expect
        # Every row still pages from its own member.
        assert {(r["source_id"], r["cells"][1][0]) for r in rows} == {(a, "a"), (b, "b")}
        # "Open as SQL" orders the same way.
        sql_rows = store.run_sql(store.spec_sql(mid, spec))
        col = sql_rows["columns"].index("source_id")
        assert [r[col] for r in sql_rows["rows"]] == expect


# ------------------------------------------------------ batch derived add

def test_batch_derived_add_fans_out_to_every_member(store, write_csv):
    a, b, mid = _mk(store, write_csv)
    res = store.add_derived_columns(mid, [
        {"name": "W1", "input_column": "Msg", "op_id": "regex_extract", "params": {"pattern": r"^(\w+)"}},
        {"name": "W2", "input_column": "Msg", "op_id": "regex_extract", "params": {"pattern": r"(\w+)$"}},
    ])
    for jid in res["job_ids"]:
        store.wait_for_ingest_job(jid, timeout=30)
    cols = [c["name"] for c in store.get_source(mid)["columns"]]
    assert "W1" in cols and "W2" in cols
    for sid in (a, b):
        assert {d["name"] for d in store.list_derived_columns(sid)} >= {"W1", "W2"}


def test_batch_add_is_all_or_nothing_across_members(store, write_csv):
    a, b, mid = _mk(store, write_csv)
    clash = store.add_derived_column(b, "Taken", "Msg", "regex_extract", {"pattern": "(a)"})
    store.wait_for_ingest_job(clash["job_id"], timeout=30)
    with pytest.raises(ValueError, match="already has a column"):
        store.add_derived_columns(mid, [
            {"name": "Fine", "input_column": "Msg", "op_id": "regex_extract", "params": {"pattern": "(a)"}},
            {"name": "Taken", "input_column": "Msg", "op_id": "regex_extract", "params": {"pattern": "(a)"}},
        ])
    assert "Fine" not in [c["name"] for c in store.get_source(a)["columns"]]


def test_a_long_in_list_filters_a_merge_on_stock_sqlite(stock_sqlite_limits, write_csv):
    """An `in` list (one per line in the filter builder; a pasted IOC list)
    bound one parameter per item, and a merge compiles the WHERE once per
    member — 3,000 hashes on an 11-member merge is 33,000 parameters, past
    stock SQLite's 32,766, and the view was a 500. The list is bound once
    per member now, whatever its length."""
    store = stock_sqlite_limits
    ids = [store.ingest_csv(write_csv([["Hash"]] + [[f"h{k}-{i}"] for i in range(5)], f"m{k}.csv"),
                            name=f"m{k}", build_fts=False)["id"] for k in range(11)]
    mid = store.create_merge("hashes", ids)["id"]
    items = [f"h{k}-0" for k in range(11)] + [f"miss-{i}" for i in range(3000)]
    for sid in (ids[0], mid):
        v = store.build_view(sid, {"source_id": sid, "filters": [], "sort": [],
                                   "filter_tree": {"type": "cond", "column": "Hash",
                                                   "op": "in", "value": items}})
        assert v["row_count"] == (1 if sid == ids[0] else 11)
    # The quick filter row's `in` takes the same path.
    v = store.build_view(mid, {"source_id": mid, "sort": [],
                               "filters": [{"column": "Hash", "op": "in", "value": "\n".join(items)}]})
    assert v["row_count"] == 11
