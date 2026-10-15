"""First/Last, round three: List values (each group's distinct values of a
column, as an output column and as `{values:Column}` in the description),
`{which:number}`, the description reading before the JSON cell, and a
nicknamed table's default result name."""

from __future__ import annotations

from pathlib import Path

import pytest

from winnow.plugin_api import PluginRegistry

PLUGIN = "first_last"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"

ROWS = [
    ["When", "Host", "User", "Bytes"],
    ["2026-03-14 08:00:00", "SRV1", "carol", "100"],
    ["2026-03-14 09:00:00", "SRV1", "alice", "9"],
    ["2026-03-14 10:00:00", "SRV1", "Bob", "100"],
    ["2026-03-14 11:00:00", "SRV1", "alice", ""],
    ["2026-03-14 12:00:00", "SRV1", "", "10"],
    ["2026-03-14 09:30:00", "SRV2", "dave", "7"],
]


@pytest.fixture(scope="module")
def example_registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    return reg


@pytest.fixture
def fl_client(client, store, write_csv, example_registry, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    rec = store.ingest_csv(write_csv(ROWS, "fl.csv"), name="fl", build_fts=False)
    return client, rec["id"]


def _fl(client, route, **body):
    r = client.post(f"/api/plugin/{PLUGIN}/{route}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _by_host(out, col):
    i = out["columns"].index(col)
    h = out["columns"].index("Host")
    return {r[h]: r[i] for r in out["rows"]}


# ------------------------------------------------------------ List values


def test_list_values_is_a_column_of_each_groups_distinct_values(fl_client):
    """Distinct, sorted without regard to case, blanks left out — and the
    group's WHOLE set, not just the two bookend rows' values."""
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=["Host"], value_columns=["User"], template="{which}")
    assert out["columns"] == ["When", "Host", "Values of User", "Description"]
    assert _by_host(out, "Values of User") == {"SRV1": "alice, Bob, carol", "SRV2": "dave"}
    # Both bookends of a group carry the same list.
    srv1 = [r for r in out["rows"] if r[1] == "SRV1"]
    assert len(srv1) == 2 and srv1[0][2] == srv1[1][2]


def test_a_number_column_lists_by_value(fl_client):
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=["Host"], value_columns=["Bytes"], template="{which}")
    # 9 < 10 < 100 by value; as text "10" < "100" < "9".
    assert _by_host(out, "Values of Bytes")["SRV1"] == "9, 10, 100"


def test_values_placeholder_takes_any_column(fl_client):
    """{values:Column} works like {Column}: the column need not be under
    List values, grouped or included first."""
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=["Host"], template="{which}: {values:User}")
    assert out["columns"] == ["When", "Host", "Description"]
    descs = sorted(r[-1] for r in out["rows"])
    assert descs == ["First: alice, Bob, carol", "Last: alice, Bob, carol", "Only: dave"]


def test_values_placeholder_with_an_unknown_column_is_a_400(fl_client):
    client, sid = fl_client
    r = client.post(f"/api/plugin/{PLUGIN}/preview", json={
        "source_id": sid, "group_by": ["Host"], "sort_column": "When", "columns": [],
        "template": "{values:Usre}"})
    assert r.status_code == 400 and "Usre" in r.text
    r = client.post(f"/api/plugin/{PLUGIN}/preview", json={
        "source_id": sid, "group_by": ["Host"], "sort_column": "When", "columns": [],
        "value_columns": ["Nope"]})
    assert r.status_code == 400 and "Nope" in r.text


def test_a_long_list_is_capped_and_says_how_many_it_left_out(fl_client, store, write_csv):
    client, _ = fl_client
    rows =[["When", "Host", "Id"]] + [
        [f"2026-03-14 08:{i // 60:02d}:{i % 60:02d}", "SRV1", f"id{i:03d}"] for i in range(60)]
    sid = store.ingest_csv(write_csv(rows, "many.csv"), name="many", build_fts=False)["id"]
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=[], value_columns=["Id"], template="{which}")
    cell = out["rows"][0][1]
    named = cell.split(" … ")[0].split(", ")
    assert len(named) == 50
    # The first fifty in order, not whichever came up first.
    assert named[0] == "id000" and named[-1] == "id049"
    assert cell.endswith("… (+10 more)")


def test_filters_scope_the_values_too(fl_client):
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=["Host"], value_columns=["User"], template="{which}",
              filters=[{"column": "User", "op": "not_in", "values": ["alice"]}])
    assert _by_host(out, "Values of User")["SRV1"] == "Bob, carol"


def test_preview_rows_and_create_agree(fl_client, store):
    client, sid = fl_client
    body = dict(source_id=sid, group_by=["Host"], sort_column="When", columns=["Host"],
                value_columns=["User"], template="{which}")
    prev = _fl(client, "preview", **body)
    whole = _fl(client, "rows", **body)
    assert whole["columns"] == prev["columns"] and whole["rows"] == prev["rows"]
    made = _fl(client, "create", name="fl-values", **body)
    src = store.get_source(made["source"]["id"])
    assert [c["name"] for c in src["columns"]] == prev["columns"]
    got = store.run_sql(f'SELECT "Values of User" FROM "{src["table_name"]}" ORDER BY rid')["rows"]
    assert [r[0] for r in got] == [r[2] for r in prev["rows"]]


def test_values_over_a_merge_including_a_null_group(fl_client, store, write_csv):
    """Invariant #9. The second member has no Host column, so its rows form
    a group whose key is NULL — the list must still find its bookends."""
    client, sid = fl_client
    sid2 = store.ingest_csv(write_csv(
        [["When", "User"], ["2026-03-15 08:00:00", "erin"], ["2026-03-15 09:00:00", "zed"]],
        "flv2.csv"), name="flv2", build_fts=False)["id"]
    mid = store.create_merge("flvm", [sid, sid2])["id"]
    out = _fl(client, "preview", source_id=mid, group_by=["Host"], sort_column="When",
              columns=["Host"], value_columns=["User"], template="{which} {values:User}")
    lists = {r[1]: r[2] for r in out["rows"]}
    assert lists["SRV1"] == "alice, Bob, carol"
    assert lists[""] == "erin, zed"


# ---------------------------------------------------------- {which:number}


def test_which_number_is_the_rows_position_in_its_group(fl_client):
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=["Host"], template="{which:number} of {count}")
    assert sorted(r[-1] for r in out["rows"]) == ["1 of 1", "1 of 5", "5 of 5"]


def test_which_with_an_unknown_option_is_a_400(fl_client):
    client, sid = fl_client
    r = client.post(f"/api/plugin/{PLUGIN}/preview", json={
        "source_id": sid, "group_by": ["Host"], "sort_column": "When", "columns": [],
        "template": "{which:roman}"})
    assert r.status_code == 400 and "which:roman" in r.text


# ------------------------------------------------------------ the result


def test_the_description_reads_before_the_json_cell(fl_client):
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=["Host"], sum_columns=["Bytes"], value_columns=["User"],
              template="{which}", row_json=True)
    assert out["columns"] == ["When", "Host", "Sum of Bytes", "Values of User",
                              "Description", "Row (JSON)"]
    assert all(r[4] in ("First", "Last", "Only") and r[5].startswith("{") for r in out["rows"])


def test_an_empty_description_falls_back_to_the_default(fl_client):
    """A new sheet starts with the box empty; the result still says
    something rather than writing a column of blanks."""
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=[], template="")
    assert sorted(r[-1] for r in out["rows"]) == ["First of 5", "Last of 5", "Only of 1"]


def test_a_nicknamed_table_names_its_result_by_the_nickname(fl_client, store):
    client, sid = fl_client
    store.set_source_nickname(sid, "Logons")
    made = _fl(client, "create", source_id=sid, group_by=["Host"], sort_column="When",
               columns=[], template="{which}")
    assert made["source"]["name"] == "First-Last of Logons"
