"""Summing a column over each group's rows.

First/Last collapses a group to its two bookends, which answers "when did
this start and stop" and throws away everything in between. A total is the
part of the middle worth keeping: bytes moved in a session, events in a
burst, minutes logged on.

It rides the window that is already partitioned by the group — the same
one behind `{count}` — so it costs no extra pass over the table.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from winnow.plugin_api import PluginRegistry

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"

ROWS = [
    ["When", "Host", "Bytes", "Note"],
    ["2026-03-14 08:00:00", "SRV1", "100", "a"],
    ["2026-03-14 09:00:00", "SRV1", "250", "b"],
    ["2026-03-14 17:30:00", "SRV1", "50.5", "c"],
    ["2026-03-14 10:00:00", "SRV2", "7", "d"],
]


@pytest.fixture(scope="module")
def example_registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    return reg


@pytest.fixture
def fl(client, store, write_csv, example_registry, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    rec = store.ingest_csv(write_csv(ROWS, "fl.csv"), name="fl", build_fts=False)
    return client, store, rec["id"]


def _fl(client, route, **body):
    r = client.post(f"/api/plugin/first_last/{route}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _err(client, route, **body):
    r = client.post(f"/api/plugin/first_last/{route}", json=body)
    assert r.status_code == 400, r.text
    return r.json()["detail"]


def test_the_total_is_the_groups_not_the_bookends(fl):
    """The number that makes this worth having: SRV1's three rows total
    400.5, not the 150.5 its two bookends carry."""
    client, _, sid = fl
    out = _fl(client, "rows", source_id=sid, group_by=["Host"], sort_column="When",
              columns=["Host"], sum_columns=["Bytes"])
    assert out["columns"] == ["When", "Host", "Sum of Bytes", "Description"]
    by_host = {r[1]: r[2] for r in out["rows"]}
    assert by_host["SRV1"] == "400.5"
    assert by_host["SRV2"] == "7"


def test_every_bookend_of_a_group_carries_its_total(fl):
    client, _, sid = fl
    out = _fl(client, "rows", source_id=sid, group_by=["Host"], sort_column="When",
              columns=["Host"], sum_columns=["Bytes"])
    srv1 = [r for r in out["rows"] if r[1] == "SRV1"]
    assert len(srv1) == 2 and {r[2] for r in srv1} == {"400.5"}


def test_a_whole_total_reads_as_a_whole_number(fl):
    """SUM is REAL so mixed ints and decimals add up; a byte count should
    still read as bytes."""
    client, _, sid = fl
    out = _fl(client, "rows", source_id=sid, group_by=["Note"], sort_column="When",
              columns=["Note"], sum_columns=["Bytes"])
    assert {r[2] for r in out["rows"]} == {"100", "250", "50.5", "7"}


def test_several_columns_at_once(fl):
    client, _, sid = fl
    out = _fl(client, "rows", source_id=sid, group_by=["Host"], sort_column="When",
              columns=[], sum_columns=["Bytes", "Bytes"])
    assert out["columns"][:3] == ["When", "Sum of Bytes", "Sum of Bytes"]


def test_a_text_column_is_refused_rather_than_summed_to_zero(fl):
    """SUM over text is 0 in SQLite, and a 0 in a report is worse than an
    error."""
    client, _, sid = fl
    assert "nothing to sum" in _err(client, "rows", source_id=sid, group_by=["Host"],
                                    sort_column="When", sum_columns=["Note"])


def test_an_unknown_column_is_refused(fl):
    client, _, sid = fl
    _err(client, "rows", source_id=sid, group_by=["Host"], sort_column="When",
         sum_columns=["Nope"])


def test_preview_and_create_agree_with_rows(fl):
    client, store, sid = fl
    args = dict(source_id=sid, group_by=["Host"], sort_column="When",
                columns=["Host"], sum_columns=["Bytes"])
    rows = _fl(client, "rows", **args)
    prev = _fl(client, "preview", **args)
    assert prev["columns"] == rows["columns"]
    made = _fl(client, "create", name="bookends", **args)
    cols = [c["name"] for c in store.get_source(made["source"]["id"])["columns"]]
    assert "Sum of Bytes" in cols
    view = store.build_view(made["source"]["id"], {})
    got = store.fetch_rows(view["view_id"], 0, 10)["rows"]
    idx = cols.index("Sum of Bytes")
    assert {r["cells"][idx] for r in got} == {"400.5", "7"}


def test_asking_for_no_sums_changes_nothing(fl):
    client, _, sid = fl
    a = _fl(client, "rows", source_id=sid, group_by=["Host"], sort_column="When", columns=["Host"])
    b = _fl(client, "rows", source_id=sid, group_by=["Host"], sort_column="When",
            columns=["Host"], sum_columns=[])
    assert a == b
