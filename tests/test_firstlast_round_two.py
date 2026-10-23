"""First/Last, round two: `{sum:Column}` (and min/max) in the description,
on any number column, `Only` for a one-row group, and the meta route
advertising the syntax."""

from __future__ import annotations

from pathlib import Path

import pytest

from winnow.plugin_api import PluginRegistry

PLUGIN = "first_last"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"

ROWS = [
    ["When", "Host", "User", "Bytes"],
    ["2026-03-14 08:00:00", "SRV1", "alice", "100"],
    ["2026-03-14 12:00:00", "SRV1", "alice", "250.5"],
    ["2026-03-14 17:30:00", "SRV1", "alice", "50"],
    ["2026-03-14 09:00:00", "SRV2", "bob", "7"],
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


def test_sum_placeholder_renders_the_group_total_formatted(fl_client):
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["User"], sort_column="When",
              columns=[], sum_columns=["Bytes"], template="{which} of {count}: {sum:Bytes} bytes")
    descs = sorted(r[-1] for r in out["rows"])
    assert descs == ["First of 3: 400.5 bytes", "Last of 3: 400.5 bytes", "Only of 1: 7 bytes"]
    # ...and the Sum column beside it says the same thing.
    assert "Sum of Bytes" in out["columns"]


def test_min_and_max_placeholders_ride_the_same_total_up_column(fl_client):
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["User"], sort_column="When",
              columns=[], sum_columns=["Bytes"], template="{which}: {min:Bytes}–{max:Bytes} of {sum:Bytes}")
    descs = sorted(r[-1] for r in out["rows"])
    assert descs == ["First: 50–250.5 of 400.5", "Last: 50–250.5 of 400.5", "Only: 7–7 of 7"]
    # Only the sum is an output column; min and max are placeholders only.
    assert [c for c in out["columns"] if c.endswith("of Bytes")] == ["Sum of Bytes"]


def test_sum_min_and_max_take_any_number_column(fl_client):
    """Like {values:Column}: the column need not be under Total up first,
    and naming it in the description adds no output column."""
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["User"], sort_column="When",
              columns=[], template="{which}: {sum:Bytes} ({min:Bytes}–{max:Bytes})")
    descs = sorted(r[-1] for r in out["rows"])
    assert descs == ["First: 400.5 (50–250.5)", "Last: 400.5 (50–250.5)", "Only: 7 (7–7)"]
    assert out["columns"] == ["When", "Description"]
    # The same numbers as the Total-up column, which is still the one way
    # to get a Sum column.
    both = _fl(client, "preview", source_id=sid, group_by=["User"], sort_column="When",
               columns=[], sum_columns=["Bytes"], template="{max:Bytes}")
    assert sorted(r[-1] for r in both["rows"]) == ["250.5", "250.5", "7"]
    whole = _fl(client, "rows", source_id=sid, group_by=["User"], sort_column="When",
                columns=[], template="{sum:Bytes}")
    assert sorted(r[-1] for r in whole["rows"]) == ["400.5", "400.5", "7"]


def test_sum_placeholder_refuses_a_text_column_and_an_unknown_one(fl_client):
    client, sid = fl_client
    for tmpl, named in (("{sum:User}", "number column"), ("{max:Host}", "number column"),
                        ("{min:Byets}", "Byets")):
        r = client.post(f"/api/plugin/{PLUGIN}/preview", json={
            "source_id": sid, "group_by": ["User"], "sort_column": "When", "columns": [],
            "template": tmpl})
        assert r.status_code == 400, tmpl
        assert named in r.text, r.text


def test_a_field_literally_named_sum_is_still_reachable_by_the_colon_rule(fl_client, store, write_csv):
    """The colon namespaces functions; plain names are always fields."""
    client, _ = fl_client
    sid = store.ingest_csv(write_csv([["When", "sum", "n"], ["2026-01-01 00:00:00", "x", "1"]], "c.csv"),
                           name="c", build_fts=False)["id"]
    out = _fl(client, "preview", source_id=sid, group_by=["n"], sort_column="When",
              columns=["sum"], template="{sum}|{which}")
    assert [r[-1] for r in out["rows"]] == ["x|Only"]


def test_single_row_groups_say_only_everywhere(fl_client):
    client, sid = fl_client
    out = _fl(client, "preview", source_id=sid, group_by=["User"], sort_column="When",
              columns=[], template="{which}")
    assert sorted(r[-1] for r in out["rows"]) == ["First", "Last", "Only"]
    rows = _fl(client, "rows", source_id=sid, group_by=["User"], sort_column="When",
               columns=[], template="{which} of {count}")
    assert "Only of 1" in {r[-1] for r in rows["rows"]}


def test_meta_advertises_the_syntax(fl_client):
    client, _ = fl_client
    meta = client.get(f"/api/plugin/{PLUGIN}/meta").json()
    assert meta["placeholders"] == ["which", "which:number", "count", "values:<column>", "unique:<column>",
                                    "sum:<column>", "min:<column>", "max:<column>"]
    assert meta["which_values"] == ["First", "Last", "Only"]
