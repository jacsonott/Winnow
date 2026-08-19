"""store.py group-by: single-level group_summary/expand_group, nested
multi-level grouping via `path`, the materialize-vs-virtual threshold, and
the virtual small-group fast path's path-scoping correctness (the exact
gap fixed last session when nested grouping was added)."""

from __future__ import annotations

from store import Store


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
