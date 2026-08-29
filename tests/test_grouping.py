"""store.py group-by: single-level group_summary/expand_group, nested
multi-level grouping via `path`, the materialize-vs-virtual threshold, and
the virtual small-group fast path's path-scoping correctness (the exact
gap fixed last session when nested grouping was added)."""

from __future__ import annotations

from winnow.store import TAG_GROUP_COLUMN, Store


def test_group_summary_count_and_value_order(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)

    by_count = store.group_summary(view["view_id"], "EventId", order="count")
    assert all(g["count"] == 1 for g in by_count["groups"])  # each EventId is unique in the fixture
    assert by_count["truncated"] is False

    by_value = store.group_summary(view["view_id"], "EventId", order="value")
    values = [g["value"] for g in by_value["groups"]]
    assert values == sorted(values, key=lambda v: float(v))  # numeric column -> numeric order


def test_group_summary_direction_swaps_both_orderings(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)

    value_asc = [g["value"] for g in store.group_summary(view["view_id"], "EventId", order="value")["groups"]]
    value_desc = [g["value"] for g in
                  store.group_summary(view["view_id"], "EventId", order="value", direction="desc")["groups"]]
    assert value_desc == list(reversed(value_asc))

    # Every EventId is unique in the fixture, so vary count via User instead
    # (ACME\\jacson appears once, but grouping by Process/User here still
    # gives distinct counts thanks to STANDARD_ROWS' shape) — use Process,
    # which is unique per row too, so pair with a duplicated-count column:
    # group by Timestamp's *day* bucket instead, where 2024-01-05 has 2 rows.
    count_desc = [(g["value"], g["count"]) for g in
                  store.group_summary(view["view_id"], "Timestamp", order="count")["groups"]]
    count_asc = [(g["value"], g["count"]) for g in
                 store.group_summary(view["view_id"], "Timestamp", order="count", direction="asc")["groups"]]
    assert count_desc[0][1] >= count_desc[-1][1]
    assert count_asc[0][1] <= count_asc[-1][1]
    assert sorted(count_desc) == sorted(count_asc)  # same groups, just reordered


def test_group_summary_datetime_buckets_by_day(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)

    res = store.group_summary(view["view_id"], "Timestamp", order="value")
    # STANDARD_ROWS: two rows on 2024-01-05, one each on -06 and -07 — full
    # "YYYY-MM-DD HH:MM:SS" timestamps would instead give four groups of 1.
    assert {g["value"]: g["count"] for g in res["groups"]} == {
        "2024-01-05": 2, "2024-01-06": 1, "2024-01-07": 1,
    }


def test_group_summary_raw_datetime_values_for_the_value_picker(ingested):
    """bucket_datetime=False returns the stored timestamps themselves, which
    is the only shape the header value-picker can hand back as an `=`/`in`
    filter — a "2024-01-05" day bucket matches no stored value at all."""
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)

    res = store.group_summary(view["view_id"], "Timestamp", order="value", bucket_datetime=False)
    assert [g["value"] for g in res["groups"]] == [
        "2024-01-05 13:22:01", "2024-01-05 13:23:11", "2024-01-06 09:15:00", "2024-01-07 22:01:59",
    ]
    assert all(g["count"] == 1 for g in res["groups"])

    # And every value it offered really does select its row back.
    for g in res["groups"]:
        picked = store.build_view(source_id, {
            "source_id": source_id,
            "filters": [{"column": "Timestamp", "op": "equals", "value": g["value"]}],
            "sort": [],
        })
        assert picked["row_count"] == 1


def test_group_summary_bucket_datetime_only_affects_datetime_columns(ingested):
    """Non-datetime columns are already raw — the flag must not perturb them,
    so a picker and a grouping ask the same question of the same column."""
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)

    bucketed = store.group_summary(view["view_id"], "Process", order="value")
    raw = store.group_summary(view["view_id"], "Process", order="value", bucket_datetime=False)
    assert bucketed == raw


def test_expand_group_datetime_bucket_finds_day_rows(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)

    exp = store.expand_group(view["view_id"], "Timestamp", "2024-01-05")
    assert exp["row_count"] == 2
    rows = store.fetch_rows(exp["view_id"], 0, 10)
    timestamps = [r["cells"][0] for r in rows["rows"]]
    assert all(ts.startswith("2024-01-05") for ts in timestamps)


def test_expand_group_datetime_bucket_materialized_path(ingested):
    """Same as above but forced through the materialize-a-real-sub-view
    branch (large group / merge) rather than the small-group virtual path —
    both branches build their WHERE off the same _eq_condition, but only
    manual review would catch one getting the DAY_BUCKET alias wrong and
    not the other."""
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)

    store.GROUP_MATERIALIZE_THRESHOLD = 0
    exp = store.expand_group(view["view_id"], "Timestamp", "2024-01-05")
    assert exp["row_count"] == 2
    rows = store.fetch_rows(exp["view_id"], 0, 10)
    timestamps = [r["cells"][0] for r in rows["rows"]]
    assert all(ts.startswith("2024-01-05") for ts in timestamps)


def test_nested_grouping_by_datetime_day_then_column(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": []}
    view = store.build_view(source_id, spec)

    inner = store.group_summary(
        view["view_id"], "User", path=[{"column": "Timestamp", "value": "2024-01-05"}],
    )
    assert {g["value"]: g["count"] for g in inner["groups"]} == {"ACME\\jacson": 1, "ACME\\admin": 1}


def test_group_summary_limit_and_truncated(store, tmp_path):
    path = tmp_path / "wide.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("K\n")
        for i in range(10):
            f.write(f"{i}\n")
    rec = store.ingest_csv(str(path), name="wide.csv", build_fts=False)
    spec = {"source_id": rec["id"], "filters": [], "sort": []}
    view = store.build_view(rec["id"], spec)
    res = store.group_summary(view["view_id"], "K", limit=3)
    assert len(res["groups"]) == 3
    assert res["truncated"] is True


def test_nested_grouping_children_sum_to_parent(store, write_csv):
    # Host x User, two hosts, overlapping user names across hosts.
    rows = [["Host", "User"]]
    rows += [["H1", "alice"]] * 3
    rows += [["H1", "bob"]] * 2
    rows += [["H2", "alice"]] * 5
    path = write_csv(rows)
    rec = store.ingest_csv(path, name="nested.csv", build_fts=False)
    spec = {"source_id": rec["id"], "filters": [], "sort": []}
    view = store.build_view(rec["id"], spec)

    top = store.group_summary(view["view_id"], "Host")
    top_by_value = {g["value"]: g["count"] for g in top["groups"]}
    assert top_by_value == {"H1": 5, "H2": 5}

    inner_h1 = store.group_summary(view["view_id"], "User", path=[{"column": "Host", "value": "H1"}])
    inner_h1_by_value = {g["value"]: g["count"] for g in inner_h1["groups"]}
    assert inner_h1_by_value == {"alice": 3, "bob": 2}
    assert sum(inner_h1_by_value.values()) == top_by_value["H1"]

    inner_h2 = store.group_summary(view["view_id"], "User", path=[{"column": "Host", "value": "H2"}])
    assert {g["value"]: g["count"] for g in inner_h2["groups"]} == {"alice": 5}


def test_expand_group_virtual_path_does_not_leak_sibling_rows(store, write_csv):
    # The exact correctness gap fixed when nested grouping was added: the
    # small-group fast path (_fetch_virtual_group_rows / _virtual_group_where)
    # must respect the outer `path`, not just the leaf column=value — H2's
    # "alice" rows must never appear when expanding H1's "alice" subgroup,
    # even though both share the literal User value "alice".
    rows = [["Host", "User", "Note"]]
    rows.append(["H1", "alice", "row-from-H1"])
    rows.append(["H2", "alice", "row-from-H2"])
    path = write_csv(rows)
    rec = store.ingest_csv(path, name="leak.csv", build_fts=False)
    spec = {"source_id": rec["id"], "filters": [], "sort": []}
    view = store.build_view(rec["id"], spec)

    expanded = store.expand_group(
        view["view_id"], "User", "alice", path=[{"column": "Host", "value": "H1"}]
    )
    assert expanded["row_count"] == 1
    handle = store._views[expanded["view_id"]]
    assert handle["kind"] == "group_virtual"  # small group -> the fast path, not a materialized view

    fetched = store.fetch_rows(expanded["view_id"], 0, 10)["rows"]
    assert len(fetched) == 1
    assert fetched[0]["cells"][2] == "row-from-H1"

    # Tagging the expanded (virtual) group must only touch that one row.
    tag = store.upsert_tag(None, "leak-test", "#ff0000", None)
    store.tag_view(expanded["view_id"], tag["id"], True)
    counts = store.tag_counts(rec["id"])
    assert counts["counts"][str(tag["id"])] == 1


def test_expand_group_materializes_for_merges_regardless_of_size(store, write_csv):
    p1 = write_csv([["Host", "User"], ["H1", "alice"]], name="m1.csv")
    p2 = write_csv([["Host", "User"], ["H1", "alice"]], name="m2.csv")
    rec1 = store.ingest_csv(p1, name="m1.csv", build_fts=False)
    rec2 = store.ingest_csv(p2, name="m2.csv", build_fts=False)
    merge = store.create_merge("merged", [rec1["id"], rec2["id"]])
    spec = {"source_id": merge["id"], "filters": [], "sort": []}
    view = store.build_view(merge["id"], spec)
    expanded = store.expand_group(view["view_id"], "Host", "H1")
    assert expanded["row_count"] == 2
    handle = store._views[expanded["view_id"]]
    assert handle["kind"] == "group"  # merge -> always a real materialized sub-view


def test_expand_group_materializes_above_threshold(store, tmp_path):
    n = Store.GROUP_MATERIALIZE_THRESHOLD + 500
    path = tmp_path / "huge_group.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("K,Idx\n")
        for i in range(n):
            f.write(f"same,{i}\n")
    rec = store.ingest_csv(str(path), name="huge_group.csv", build_fts=False)
    spec = {"source_id": rec["id"], "filters": [], "sort": []}
    view = store.build_view(rec["id"], spec)
    expanded = store.expand_group(view["view_id"], "K", "same")
    assert expanded["row_count"] == n
    handle = store._views[expanded["view_id"]]
    assert handle["kind"] == "group"


def _search_grouped_fixture(store, write_csv):
    """Two rows sharing a group value, only one of which matches the search."""
    rows = [["Host", "User", "Note"]]
    rows.append(["H1", "alice", "needle-one"])
    rows.append(["H1", "alice", "haystack"])
    rows.append(["H2", "bob", "needle-two"])
    path = write_csv(rows, name="searched.csv")
    rec = store.ingest_csv(path, name="searched.csv", build_fts=False)
    spec = {"source_id": rec["id"], "filters": [], "sort": [], "search": "needle"}
    return rec["id"], store.build_view(rec["id"], spec)


def test_expand_group_under_search_pages_only_matching_rows(store, write_csv):
    # The small-group fast path can't express the parent view's search (it
    # reads the member table directly), so a filtered parent has to
    # materialize. Before that gate, the count was right and the rows were
    # wrong: 1 row reported, "haystack" served as the first of two.
    source_id, view = _search_grouped_fixture(store, write_csv)
    assert view["row_count"] == 2

    summary = {g["value"]: g["count"] for g in store.group_summary(view["view_id"], "User")["groups"]}
    assert summary == {"alice": 1, "bob": 1}

    expanded = store.expand_group(view["view_id"], "User", "alice")
    assert expanded["row_count"] == 1
    assert store._views[expanded["view_id"]]["kind"] == "group"  # filtered parent -> materialized

    fetched = store.fetch_rows(expanded["view_id"], 0, 10)["rows"]
    assert [r["cells"][2] for r in fetched] == ["needle-one"]


def test_tag_group_under_search_skips_non_matching_rows(store, write_csv):
    # Same gap, but the expensive half: tagging a group off the virtual path
    # tagged every row sharing the group value, search or no search.
    source_id, view = _search_grouped_fixture(store, write_csv)
    expanded = store.expand_group(view["view_id"], "User", "alice")
    tag = store.upsert_tag(None, "search-scope", "#00ff00", None)
    store.tag_view(expanded["view_id"], tag["id"], True)
    assert store.tag_counts(source_id)["counts"][str(tag["id"])] == 1


def test_expand_group_stays_virtual_on_an_unfiltered_parent(store, write_csv):
    # The fast path is still the fast path where it's sound — a small group
    # of a parent that provably holds every row of the source.
    rows = [["Host", "User"], ["H1", "alice"], ["H1", "alice"], ["H2", "bob"]]
    path = write_csv(rows, name="unfiltered.csv")
    rec = store.ingest_csv(path, name="unfiltered.csv", build_fts=False)
    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": [{"column": "User"}]})
    expanded = store.expand_group(view["view_id"], "User", "alice")
    assert store._views[expanded["view_id"]]["kind"] == "group_virtual"
    assert expanded["row_count"] == 2


# ------------------------------------------------------------- group by tag

def _tagged_fixture(store, write_csv):
    """10 rows; rids 1-3 tagged Alpha, rids 3-4 tagged Beta, 5-10 untagged."""
    rows = [["Host", "User"]] + [[f"H{i % 2}", "alice"] for i in range(10)]
    rec = store.ingest_csv(write_csv(rows, name="tagged.csv"), name="tagged.csv", build_fts=False)
    alpha = store.upsert_tag(None, "Alpha", "#ff0000", "1")
    beta = store.upsert_tag(None, "Beta", "#00ff00", "2")
    store.set_tags(rec["id"], [1, 2, 3], alpha["id"], True)
    store.set_tags(rec["id"], [3, 4], beta["id"], True)
    return rec["id"], alpha, beta


def test_group_by_tag_counts_each_tag_and_the_untagged_remainder(store, write_csv):
    source_id, alpha, beta = _tagged_fixture(store, write_csv)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    res = store.group_summary(view["view_id"], TAG_GROUP_COLUMN)
    by_value = {g["value"]: g["count"] for g in res["groups"]}
    # A row with two tags is counted under both, so these sum to more than
    # the 10 rows in the view — that's the point of grouping by tag.
    assert by_value == {alpha["id"]: 3, beta["id"]: 2, None: 6}


def test_group_by_tag_omits_the_untagged_group_when_every_row_is_tagged(store, write_csv):
    rows = [["Host"], ["H1"], ["H2"]]
    rec = store.ingest_csv(write_csv(rows, name="alltagged.csv"), name="alltagged.csv", build_fts=False)
    tag = store.upsert_tag(None, "Everything", "#123456", None)
    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": []})
    store.tag_view(view["view_id"], tag["id"], True)
    res = store.group_summary(view["view_id"], TAG_GROUP_COLUMN)
    assert [g["value"] for g in res["groups"]] == [tag["id"]]


def test_group_by_tag_orders_by_name_not_by_id(store, write_csv):
    rows = [["Host"], ["H1"], ["H2"], ["H3"]]
    rec = store.ingest_csv(write_csv(rows, name="named.csv"), name="named.csv", build_fts=False)
    zeta = store.upsert_tag(None, "Zeta", "#111111", None)   # lower id, later name
    alpha = store.upsert_tag(None, "Alpha", "#222222", None)
    store.set_tags(rec["id"], [1, 2, 3], zeta["id"], True)
    store.set_tags(rec["id"], [1], alpha["id"], True)
    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": []})
    ordered = store.group_summary(view["view_id"], TAG_GROUP_COLUMN, order="value")["groups"]
    assert [g["value"] for g in ordered if g["value"] is not None] == [alpha["id"], zeta["id"]]


def test_group_by_tag_scopes_to_the_filtered_view(store, write_csv):
    source_id, alpha, beta = _tagged_fixture(store, write_csv)
    # H1 rows are rids 2, 4, 6, 8, 10 — Alpha has one of them, Beta one.
    view = store.build_view(source_id, {
        "source_id": source_id, "filters": [{"column": "Host", "op": "equals", "value": "H1"}], "sort": [],
    })
    by_value = {g["value"]: g["count"] for g in store.group_summary(view["view_id"], TAG_GROUP_COLUMN)["groups"]}
    assert by_value == {alpha["id"]: 1, beta["id"]: 1, None: 3}


def test_expand_tag_group_returns_that_tags_rows(store, write_csv):
    source_id, alpha, beta = _tagged_fixture(store, write_csv)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    exp = store.expand_group(view["view_id"], TAG_GROUP_COLUMN, alpha["id"])
    assert exp["row_count"] == 3
    assert [r["rid"] for r in store.fetch_rows(exp["view_id"], 0, 20)["rows"]] == [1, 2, 3]


def test_expand_the_untagged_group(store, write_csv):
    source_id, alpha, beta = _tagged_fixture(store, write_csv)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    exp = store.expand_group(view["view_id"], TAG_GROUP_COLUMN, None)
    assert exp["row_count"] == 6
    assert [r["rid"] for r in store.fetch_rows(exp["view_id"], 0, 20)["rows"]] == [5, 6, 7, 8, 9, 10]


def test_tag_a_tag_group_both_directions(store, write_csv):
    """Tagging/untagging a tag group reads row_tags in the subquery and
    writes row_tags in the same statement — the shape most likely to be
    quietly wrong."""
    source_id, alpha, beta = _tagged_fixture(store, write_csv)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    gamma = store.upsert_tag(None, "Gamma", "#0000ff", None)

    alpha_group = store.expand_group(view["view_id"], TAG_GROUP_COLUMN, alpha["id"])
    store.tag_view(alpha_group["view_id"], gamma["id"], True)
    assert store.tag_counts(source_id)["counts"][str(gamma["id"])] == 3

    # Untagging Alpha off the Alpha group empties it.
    store.tag_view(alpha_group["view_id"], alpha["id"], False)
    assert str(alpha["id"]) not in store.tag_counts(source_id)["counts"]
    assert store.tag_counts(source_id)["counts"][str(gamma["id"])] == 3  # untouched


def test_nested_grouping_with_a_tag_as_the_outer_level(store, write_csv):
    source_id, alpha, beta = _tagged_fixture(store, write_csv)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    path = [{"column": TAG_GROUP_COLUMN, "value": alpha["id"]}]
    inner = store.group_summary(view["view_id"], "Host", path=path)
    # Alpha is rids 1,2,3 -> H0 (rid 1, 3), H1 (rid 2)
    assert {g["value"]: g["count"] for g in inner["groups"]} == {"H0": 2, "H1": 1}
    sub = store.expand_group(view["view_id"], "Host", "H0", path=path)
    assert [r["rid"] for r in store.fetch_rows(sub["view_id"], 0, 20)["rows"]] == [1, 3]


def test_nested_grouping_with_a_tag_as_the_inner_level(store, write_csv):
    source_id, alpha, beta = _tagged_fixture(store, write_csv)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    path = [{"column": "Host", "value": "H0"}]
    inner = store.group_summary(view["view_id"], TAG_GROUP_COLUMN, path=path)
    # H0 is the odd rids 1,3,5,7,9 — Alpha holds 1 and 3, Beta holds 3.
    assert {g["value"]: g["count"] for g in inner["groups"]} == {alpha["id"]: 2, beta["id"]: 1, None: 3}
    sub = store.expand_group(view["view_id"], TAG_GROUP_COLUMN, beta["id"], path=path)
    assert [r["rid"] for r in store.fetch_rows(sub["view_id"], 0, 20)["rows"]] == [3]


def test_group_by_tag_on_a_merge(store, write_csv):
    p1 = write_csv([["Host"], ["H1"], ["H2"]], name="mt1.csv")
    p2 = write_csv([["Host"], ["H3"], ["H4"]], name="mt2.csv")
    rec1 = store.ingest_csv(p1, name="mt1.csv", build_fts=False)
    rec2 = store.ingest_csv(p2, name="mt2.csv", build_fts=False)
    tag = store.upsert_tag(None, "Across", "#abcdef", None)
    store.set_tags(rec1["id"], [1], tag["id"], True)
    store.set_tags(rec2["id"], [2], tag["id"], True)
    merge = store.create_merge("merged", [rec1["id"], rec2["id"]])
    view = store.build_view(merge["id"], {"source_id": merge["id"], "filters": [], "sort": []})
    by_value = {g["value"]: g["count"] for g in store.group_summary(view["view_id"], TAG_GROUP_COLUMN)["groups"]}
    # row_tags is keyed by (source_id, rid), so a merge has to test each
    # member against its own source id — rid 1 of one member and rid 2 of
    # the other, not both rids on both.
    assert by_value == {tag["id"]: 2, None: 2}
    exp = store.expand_group(view["view_id"], TAG_GROUP_COLUMN, tag["id"])
    assert exp["row_count"] == 2
    got = {(r["source_id"], r["rid"]) for r in store.fetch_rows(exp["view_id"], 0, 20)["rows"]}
    assert got == {(rec1["id"], 1), (rec2["id"], 2)}


def test_tag_pseudo_column_name_is_reserved_at_ingest(store, write_csv):
    """A CSV that genuinely has a `__tag__` header must not shadow the
    pseudo-column — sanitize_columns renames it, same as it does `rid`."""
    rec = store.ingest_csv(
        write_csv([[TAG_GROUP_COLUMN, "Host"], ["x", "H1"]], name="collide.csv"),
        name="collide.csv", build_fts=False,
    )
    names = [c["name"] for c in store.get_source(rec["id"])["columns"]]
    assert TAG_GROUP_COLUMN not in names
    assert names[0] == TAG_GROUP_COLUMN + "_1"


def test_group_by_tag_never_scans_the_view_per_tagged_row(store, write_csv):
    """The plan, not just the answer. `v.view_N` is indexed on pos and
    nothing else, so reaching a view row by rid is a full scan of it —
    and given a `WHERE vv.source_id = ?` to work with, SQLite will happily
    drive from row_tags' covering index and re-scan the whole view once per
    tagged row. That plan returns the right numbers and took minutes where
    the right one takes milliseconds, so only EXPLAIN catches it. The
    CROSS JOIN in _tag_group_branches is what pins it."""
    source_id, alpha, beta = _tagged_fixture(store, write_csv)
    view = store.build_view(source_id, {
        "source_id": source_id, "filters": [{"column": "Host", "op": "equals", "value": "H1"}], "sort": [],
    })
    branches, _ = store._tag_group_branches(
        view["view_id"], store._source_lite(source_id),
        store._resolve_members(source_id), False, {}, lambda m, d: ("", []),
    )
    tagged = next(b for b in branches if "row_tags rt " in b)
    with store._reader() as ro:
        steps = [r[3].upper() for r in ro.execute("EXPLAIN QUERY PLAN " + tagged)]
    plan = " | ".join(steps)
    # The view is the outer loop and row_tags is probed by its primary key,
    # never the other way round.
    assert steps[0].startswith("SCAN VV"), plan
    assert "SEARCH RT USING PRIMARY KEY" in plan, plan


def test_group_by_tag_on_a_whole_table_never_touches_the_source(store, write_csv):
    """The unfiltered case is the common one (group by tag on a table you
    just opened), and it's answerable from row_tags plus the row count
    alone — no scan of the source table at all."""
    source_id, alpha, beta = _tagged_fixture(store, write_csv)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    src = store._source_lite(source_id)
    branches, params = store._tag_group_branches(
        view["view_id"], src, store._resolve_members(source_id), True,
        {source_id: src["row_count"]}, lambda m, d: ("", []),
    )
    assert params == []
    assert not any(src["table_name"] in b for b in branches)


def test_undo_survives_the_group_view_it_was_tagged_through(store, write_csv):
    """Tagging a group from its header menu builds a throwaway sub-view and
    drops it straight after (groupRowsView/release in app.js). Undo records
    the *rows* in a v.undo_<n> table rather than the view, so dropping the
    view mustn't take the undo entry with it."""
    source_id, alpha, beta = _tagged_fixture(store, write_csv)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    gamma = store.upsert_tag(None, "Gamma", "#00ffff", None)

    grp = store.expand_group(view["view_id"], "Host", "H0")
    store.tag_view(grp["view_id"], gamma["id"], True)
    tagged = store.tag_counts(source_id)["counts"][str(gamma["id"])]
    assert tagged == 5

    store.close_view(grp["view_id"])  # what the menu's release() does
    store.undo_last_tag_change()
    assert str(gamma["id"]) not in store.tag_counts(source_id)["counts"]


def test_undo_a_tag_applied_to_a_tag_group(store, write_csv):
    # The most exotic combination: grouped by tag, tag one of those groups,
    # then undo. The group's rows are found through row_tags and the change
    # is written to row_tags, so the delta has to be materialised before the
    # write — which _apply_tag_change does by construction.
    source_id, alpha, beta = _tagged_fixture(store, write_csv)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    gamma = store.upsert_tag(None, "Gamma", "#00ffff", None)

    alpha_group = store.expand_group(view["view_id"], TAG_GROUP_COLUMN, alpha["id"])
    assert alpha_group["row_count"] == 3
    store.tag_view(alpha_group["view_id"], gamma["id"], True)
    assert store.tag_counts(source_id)["counts"][str(gamma["id"])] == 3

    store.undo_last_tag_change()
    counts = store.tag_counts(source_id)["counts"]
    assert str(gamma["id"]) not in counts
    assert counts[str(alpha["id"])] == 3  # Alpha itself untouched by the undo


def test_untagging_a_tag_group_removes_exactly_that_tag(store, write_csv):
    """Untagging tag X off the group of rows tagged X reads row_tags in the
    target query and deletes from row_tags in the same statement."""
    source_id, alpha, beta = _tagged_fixture(store, write_csv)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    alpha_group = store.expand_group(view["view_id"], TAG_GROUP_COLUMN, alpha["id"])
    store.tag_view(alpha_group["view_id"], alpha["id"], False)
    counts = store.tag_counts(source_id)["counts"]
    assert str(alpha["id"]) not in counts
    assert counts[str(beta["id"])] == 2  # rid 3 keeps Beta

    store.undo_last_tag_change()
    assert store.tag_counts(source_id)["counts"][str(alpha["id"])] == 3
