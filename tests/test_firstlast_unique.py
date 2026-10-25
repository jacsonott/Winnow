"""First/Last's Count unique: a column under Count unique becomes each
group's number of distinct non-blank values, and `{unique:Column}` gives
the same count for any column in the description. Both ride the grouping
pass rather than a second one (see the module docstring)."""

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


def test_count_unique_is_a_column_of_each_groups_distinct_count(fl_client):
    """The group's WHOLE set, not the two bookends: SRV1's bookends are
    carol and the blank row, and it has three users. Blanks are not
    counted, as List values leaves them out."""
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=["Host"], unique_columns=["User"], template="{which}")
    assert out["columns"] == ["When", "Host", "Unique count of User", "Description"]
    assert _by_host(out, "Unique count of User") == {"SRV1": "3", "SRV2": "1"}
    srv1 = [r for r in out["rows"] if r[1] == "SRV1"]
    assert len(srv1) == 2 and srv1[0][2] == srv1[1][2]


def test_the_count_is_how_many_values_list_values_lists(fl_client):
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=["Host"], unique_columns=["User", "Bytes"], value_columns=["User", "Bytes"],
              template="{which}")
    assert out["columns"] == ["When", "Host", "Unique count of User", "Unique count of Bytes",
                              "Values of User", "Values of Bytes", "Description"]
    for col in ("User", "Bytes"):
        counts = _by_host(out, f"Unique count of {col}")
        lists = _by_host(out, f"Values of {col}")
        assert {h: str(len(v.split(", "))) for h, v in lists.items()} == counts


def test_unique_placeholder_takes_any_column(fl_client):
    """{unique:Column} works like {values:Column}: nothing has to be under
    Count unique, and it adds no output column."""
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=["Host"], template="{which}: {unique:User} users, {unique:Bytes} sizes")
    assert out["columns"] == ["When", "Host", "Description"]
    descs = sorted(r[-1] for r in out["rows"])
    assert descs == ["First: 3 users, 3 sizes", "Last: 3 users, 3 sizes", "Only: 1 users, 1 sizes"]


def test_an_unknown_column_is_a_400(fl_client):
    client, sid = fl_client
    for extra, named in (({"template": "{unique:Usre}"}, "Usre"), ({"unique_columns": ["Nope"]}, "Nope"),
                         ({"unique_columns": "User"}, "unique_columns")):
        r = client.post(f"/api/plugin/{PLUGIN}/preview", json={
            "source_id": sid, "group_by": ["Host"], "sort_column": "When", "columns": [], **extra})
        assert r.status_code == 400 and named in r.text, r.text


def test_filters_scope_the_count_too(fl_client):
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=["Host"], unique_columns=["User"], template="{which}",
              filters=[{"column": "User", "op": "not_in", "values": ["alice"]}])
    assert _by_host(out, "Unique count of User")["SRV1"] == "2"


def test_a_number_sort_column_counts_the_same(fl_client):
    """A number-typed sort column takes _groups_sql's other path."""
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="Bytes",
              columns=["Host"], unique_columns=["User"], template="{which} {unique:User}")
    assert _by_host(out, "Unique count of User") == {"SRV1": "3", "SRV2": "1"}
    assert {r[-1] for r in out["rows"] if r[1] == "SRV1"} == {"First 3", "Last 3"}


def test_preview_rows_and_create_agree(fl_client, store):
    client, sid = fl_client
    body = dict(source_id=sid, group_by=["Host"], sort_column="When", columns=["Host"],
                unique_columns=["User"], template="{which} of {count}, {unique:Bytes} sizes")
    prev = _fl(client, "preview", **body)
    whole = _fl(client, "rows", **body)
    assert whole["columns"] == prev["columns"] and whole["rows"] == prev["rows"]
    made = _fl(client, "create", name="fl-unique", **body)
    src = store.get_source(made["source"]["id"])
    assert [c["name"] for c in src["columns"]] == prev["columns"]
    got = store.run_sql(f'SELECT "Unique count of User", "Description" FROM "{src["table_name"]}" ORDER BY rid')["rows"]
    assert [list(r) for r in got] == [[r[2], r[3]] for r in prev["rows"]]


def test_count_unique_over_a_merge_including_a_null_group(fl_client, store, write_csv):
    """Invariant #9. The second member has no Host column, so its rows form
    a group whose key is NULL, and its users are counted all the same."""
    client, sid = fl_client
    sid2 = store.ingest_csv(write_csv(
        [["When", "User"], ["2026-03-15 08:00:00", "erin"], ["2026-03-15 09:00:00", "zed"],
         ["2026-03-15 10:00:00", "erin"]], "flu2.csv"), name="flu2", build_fts=False)["id"]
    mid = store.create_merge("flum", [sid, sid2])["id"]
    out = _fl(client, "preview", source_id=mid, group_by=["Host"], sort_column="When",
              columns=["Host"], unique_columns=["User"], template="{which} {unique:User}")
    counts = {r[1]: r[2] for r in out["rows"]}
    assert counts["SRV1"] == "3" and counts["SRV2"] == "1"
    assert counts[""] == "2"


def test_a_source_column_named_like_the_output_is_not_read_in_its_place(fl_client, store, write_csv):
    """_reserved_aliases: a file column called "Unique count of User" must
    not be projected for the template beside the pass's own count."""
    client, _ = fl_client
    sid = store.ingest_csv(write_csv(
        [["When", "Host", "User", "Unique count of User"],
         ["2026-03-14 08:00:00", "SRV1", "a", "999"],
         ["2026-03-14 09:00:00", "SRV1", "b", "999"]], "clash.csv"), name="clash", build_fts=False)["id"]
    out = _fl(client, "preview", source_id=sid, group_by=["Host"], sort_column="When",
              columns=[], unique_columns=["User"], template="{which} {unique:User}")
    assert [r[-1] for r in out["rows"]] == ["First 2", "Last 2"]
    assert {r[1] for r in out["rows"]} == {"2"}
