"""Store.time_histogram — time buckets over the CURRENT view (filtered,
merged, root_virtual alike) — and the table_histogram example plugin
that exposes it through a toolbar panel + route, registered via the new
register_toolbar_panel hook."""

from __future__ import annotations

from pathlib import Path

import pytest

from winnow.plugin_api import PluginRegistry

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"

ROWS = [["When", "Host"]] + [
    [f"2026-03-14 08:{m:02d}:00", "A" if (m // 2) % 2 else "B"] for m in range(0, 60, 2)   # 30 rows, 2 min apart, hosts alternating
] + [["not a date", "A"], ["", "B"]]


@pytest.fixture
def src(store, write_csv):
    return store.ingest_csv(write_csv(ROWS, "h.csv"), name="h", build_fts=False)["id"]


def test_unfiltered_view_buckets_the_whole_table(store, src):
    view = store.build_view(src, {"source_id": src})
    h = store.time_histogram(view["view_id"], "When")
    assert h["total"] == 30                       # the two unparsable rows fall out
    assert h["start"] == "2026-03-14 08:00:00" and h["end"] == "2026-03-14 08:58:00"
    assert sum(n for _, n in h["buckets"]) == 30
    # 58 min span / 160 max buckets → 30-second buckets.
    assert h["bucket_seconds"] == 30
    assert all(b % 30 == 0 for b, _ in h["buckets"])


def test_filtered_view_only_counts_its_rows(store, src):
    view = store.build_view(src, {"source_id": src, "filters": [{"column": "Host", "op": "equals", "value": "A"}]})
    h = store.time_histogram(view["view_id"], "When")
    assert h["total"] == 15
    assert sum(n for _, n in h["buckets"]) == 15


def test_merge_view_unions_members(store, src, write_csv):
    sid2 = store.ingest_csv(write_csv([["When", "Host"], ["2026-03-14 09:00:00", "Z"]], "h2.csv"),
                            name="h2", build_fts=False)["id"]
    mid = store.create_merge("hm", [src, sid2])["id"]
    view = store.build_view(mid, {"source_id": mid})
    h = store.time_histogram(view["view_id"], "When")
    assert h["total"] == 31 and h["end"] == "2026-03-14 09:00:00"


def test_bucket_width_fits_max_buckets(store, src):
    view = store.build_view(src, {"source_id": src})
    h = store.time_histogram(view["view_id"], "When", max_buckets=4)
    # 58 min into ≤4 bars → 15-minute buckets.
    assert h["bucket_seconds"] == 900 and len(h["buckets"]) <= 4


def test_non_datetime_column_is_refused_and_empty_view_is_empty(store, src):
    view = store.build_view(src, {"source_id": src})
    with pytest.raises(ValueError, match="not a datetime"):
        store.time_histogram(view["view_id"], "Host")
    none = store.build_view(src, {"source_id": src, "filters": [{"column": "Host", "op": "equals", "value": "nope"}]})
    assert store.time_histogram(none["view_id"], "When")["total"] == 0


def test_plugin_registers_a_toolbar_panel_and_its_route(client, store, src, monkeypatch):
    import server
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    rec = next(p for p in reg.describe() if p["fs_name"] == "table_histogram")
    assert rec["error"] is None, rec["error"]
    monkeypatch.setattr(server, "PLUGINS", reg)
    panels = client.get("/api/plugins").json()["panels"]
    assert any(p["id"] == "table-histogram.histogram" and p["entry"] == "ui/panel.js" for p in panels)
    view = store.build_view(src, {"source_id": src})
    r = client.post("/api/plugin/table_histogram/histogram", json={"view_id": view["view_id"], "column": "When"})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 30
    r = client.post("/api/plugin/table_histogram/histogram", json={"view_id": "nope", "column": "When"})
    assert r.status_code == 400


def test_panel_registration_validates(tmp_path):
    d = tmp_path / "badpanel"
    d.mkdir()
    (d / "__init__.py").write_text(
        "def register(api):\n    api.register_toolbar_panel(id='p', label='x', entry='ui/missing.js')\n")
    reg = PluginRegistry()
    reg.load([tmp_path])
    rec = next(p for p in reg.describe() if p["fs_name"] == "badpanel")
    assert rec["error"] and "inside the plugin folder" in rec["error"]
