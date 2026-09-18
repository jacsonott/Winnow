"""Store.time_histogram — time buckets over the CURRENT view (filtered,
merged, root_virtual alike) — and the GET /api/histogram route the
built-in histogram strip (static/js/histogram.js) draws from, plus the
tombstone that keeps a stale copy of the retired table_histogram example
from loading beside it."""

from __future__ import annotations

import json
import textwrap

import pytest

from winnow.plugin_api import PluginRegistry

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


# ---------------------------------------------------------------- route

def test_route_returns_the_buckets_of_the_view(client, store, src):
    view = store.build_view(src, {"source_id": src, "filters": [{"column": "Host", "op": "equals", "value": "A"}]})
    r = client.get(f"/api/histogram?view_id={view['view_id']}&column=When")
    assert r.status_code == 200, r.text
    h = r.json()
    assert h["total"] == 15 and h["column"] == "When" and sum(n for _, n in h["buckets"]) == 15


def test_route_serves_a_merge_view(client, store, src, write_csv):
    sid2 = store.ingest_csv(write_csv([["When", "Host"], ["2026-03-14 09:00:00", "Z"]], "h2.csv"),
                            name="h2", build_fts=False)["id"]
    mid = store.create_merge("hm", [src, sid2])["id"]
    view = store.build_view(mid, {"source_id": mid})
    r = client.get(f"/api/histogram?view_id={view['view_id']}&column=When")
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 31 and r.json()["end"] == "2026-03-14 09:00:00"


def test_route_clamps_the_bucket_count_to_what_a_canvas_can_show(client, store, src):
    view = store.build_view(src, {"source_id": src})
    # 58 min at "one bar per second" would be 3,480 bars; 400 is the ceiling → 10s buckets.
    r = client.get(f"/api/histogram?view_id={view['view_id']}&column=When&max_buckets=100000")
    assert r.status_code == 200 and r.json()["bucket_seconds"] == 10
    # And below the floor of 20: 58 min into ≤20 bars → 5-minute buckets.
    r = client.get(f"/api/histogram?view_id={view['view_id']}&column=When&max_buckets=1")
    assert r.status_code == 200 and r.json()["bucket_seconds"] == 300


def test_route_400s_a_column_it_cannot_chart(client, store, src):
    """Both are the analyst's problem, not the view's: a 409 here would have
    the strip waiting for a view change that fixes nothing."""
    view = store.build_view(src, {"source_id": src})
    r = client.get(f"/api/histogram?view_id={view['view_id']}&column=Host")
    assert r.status_code == 400 and "not a datetime" in r.json()["detail"]
    r = client.get(f"/api/histogram?view_id={view['view_id']}&column=Missing")
    assert r.status_code == 400
    assert "Missing" in r.json()["detail"] and "expired" not in r.json()["detail"].lower()


def test_route_409s_an_expired_view(client, store, src):
    """The /api/rows contract: the strip treats an 'expired' 409 as a
    rebuild in progress and waits for the view change that ends it."""
    r = client.get("/api/histogram?view_id=nope&column=When")
    assert r.status_code == 409 and "expired" in r.json()["detail"].lower()
    view = store.build_view(src, {"source_id": src})
    store.close_view(view["view_id"])
    r = client.get(f"/api/histogram?view_id={view['view_id']}&column=When")
    assert r.status_code == 409 and "expired" in r.json()["detail"].lower()


# ------------------------------------------------------------ tombstone

STALE_EXAMPLE = """
    PLUGIN = {"name": "table-histogram", "version": "1.0.0", "description": "stale copy"}

    def register(api):
        api.register_toolbar_panel(id="histogram", label="Histogram", entry="ui/panel.js")
"""


def _copy_of_the_example(root):
    (root / "table_histogram" / "ui").mkdir(parents=True)
    (root / "table_histogram" / "__init__.py").write_text(textwrap.dedent(STALE_EXAMPLE))
    (root / "table_histogram" / "ui" / "panel.js").write_text("export default function mount() {}\n")


def _plugins_from(monkeypatch, installed, bundled):
    import server

    monkeypatch.setattr(server, "PLUGINS", PluginRegistry())
    monkeypatch.setattr(server, "PLUGIN_DIRS", [installed, bundled])
    monkeypatch.setattr(server, "BUNDLED_PLUGIN_DIR", bundled)
    server._reload_plugins()
    return server


def test_a_stale_copy_of_the_retired_example_is_never_enabled(client, store, tmp_path, monkeypatch):
    """A zip install that never ran an update keeps examples/plugins/
    table_histogram beside the built-in strip. The folder is still listed —
    it is the thing to delete — with the reason in its row; the toggle
    refuses it in either scope and persists nothing; and a case file that
    already carries an override for it passes through the same policy the
    tombstone sits in, so it cannot bring back a second Histogram button."""
    bundled, installed = tmp_path / "bundled", tmp_path / "plugins"
    _copy_of_the_example(bundled)
    installed.mkdir()
    server = _plugins_from(monkeypatch, installed, bundled)

    def state():
        r = client.get("/api/plugins").json()
        rec = next(p for p in r["plugins"] if p["fs_name"] == "table_histogram")
        return rec["enabled"], rec["error"], [p["id"] for p in r["panels"]]

    enabled, why, panels = state()
    assert (enabled, panels) == (False, [])
    assert why and "built into Winnow" in why and "examples/plugins/table_histogram" in why

    r = client.post("/api/plugins/toggle", json={"fs_name": "table_histogram", "scope": "on_all"})
    assert r.status_code == 400 and "built into Winnow" in r.json()["detail"]
    assert "table_histogram" not in server.WS.plugin_prefs.enabled_bundled()
    r = client.post("/api/plugins/toggle", json={"fs_name": "table_histogram", "scope": "on_case"})
    assert r.status_code == 400
    assert store.get_case_settings().get("plugin_overrides") is None
    assert state()[0] is False and state()[2] == []

    store.set_case_setting("plugin_overrides", json.dumps({"table_histogram": True}))
    server._reload_plugins()
    assert state()[0] is False and state()[2] == []


def test_an_analysts_own_copy_of_the_example_is_theirs(client, tmp_path, monkeypatch):
    """The tombstone is about the shipped folder. The same code installed
    into plugins/ is the analyst's plugin: it loads, and its toggle is an
    ordinary toggle."""
    bundled, installed = tmp_path / "bundled", tmp_path / "plugins"
    bundled.mkdir()
    _copy_of_the_example(installed)
    _plugins_from(monkeypatch, installed, bundled)

    listing = client.get("/api/plugins").json()
    rec = next(p for p in listing["plugins"] if p["fs_name"] == "table_histogram")
    assert rec["enabled"] is True and rec["error"] is None and not rec["bundled"]
    assert [p["id"] for p in listing["panels"]] == ["table-histogram.histogram"]
    r = client.post("/api/plugins/toggle", json={"fs_name": "table_histogram", "scope": "off_all"})
    assert r.status_code == 200, r.text
    assert next(p for p in r.json()["plugins"] if p["fs_name"] == "table_histogram")["enabled"] is False
