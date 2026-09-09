"""The first_last plugin's result actions: the `rows` route returns the
ENTIRE result (Copy result copies what Create would make, not what the
preview happened to show), and `create` with `timeline_tag` lands the
table AND tags every row — which is what puts bookends on the unified
Timeline — through the public set_tags path (invariant #7: undo works)."""

from __future__ import annotations

from pathlib import Path

import pytest

from winnow.plugin_api import PluginRegistry

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"


@pytest.fixture(scope="module")
def example_registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    return reg


FL_ROWS = [
    ["When", "Host", "User"],
    ["2026-03-14 08:00:00", "SRV1", "alice"],
    ["2026-03-14 09:00:00", "SRV1", "alice"],
    ["2026-03-14 17:30:00", "SRV1", "alice"],
    ["2026-03-14 10:00:00", "SRV2", "bob"],
    ["2026-03-14 11:00:00", "SRV1", "carol"],
    ["2026-03-14 12:00:00", "SRV1", "carol"],
]


@pytest.fixture
def fl2(client, store, write_csv, example_registry, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", example_registry)
    rec = store.ingest_csv(write_csv(FL_ROWS, "fl2.csv"), name="fl2", build_fts=False)
    return client, store, rec["id"]


def _fl(client, route, **body):
    r = client.post(f"/api/plugin/first_last/{route}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_rows_returns_the_whole_result(fl2):
    client, store, sid = fl2
    out = _fl(client, "rows", source_id=sid, group_by=["Host", "User"],
              sort_column="When", columns=["Host"])
    assert out["columns"] == ["When", "Host", "Description"]
    # 3 groups: alice (2 bookends), bob (1 — single row), carol (2) = 5 rows,
    # more than any preview would show.
    assert len(out["rows"]) == 5
    assert out["truncated"] is False


def test_create_with_timeline_tag_tags_every_row(fl2):
    client, store, sid = fl2
    out = _fl(client, "create", source_id=sid, group_by=["Host", "User"],
              sort_column="When", columns=["User"], name="bookends",
              timeline_tag="Session bookends")
    new_id = out["source"]["id"]
    assert out["source"]["row_count"] == 5
    assert out["timeline_tag"]["name"] == "Session bookends"
    tag = next(t for t in store.list_tags() if t["name"] == "Session bookends")
    assert out["timeline_tag"]["id"] == tag["id"]
    counts = store.tag_counts(new_id)["counts"]
    assert counts[str(tag["id"])] == 5
    # …and the write went through the undoable path.
    undone = store.undo_last_tag_change()
    assert undone["affected"] == 5
    assert store.tag_counts(new_id)["counts"].get(str(tag["id"]), 0) == 0


def test_create_reuses_an_existing_tag_by_name(fl2):
    client, store, sid = fl2
    existing = store.upsert_tag(None, "Exfil window", "#ff0000", None)
    out = _fl(client, "create", source_id=sid, group_by=["Host"],
              sort_column="When", columns=[], timeline_tag="Exfil window")
    assert out["timeline_tag"]["id"] == existing["id"]
    names = [t["name"] for t in store.list_tags()]
    assert names.count("Exfil window") == 1  # reused, not duplicated


def test_create_without_timeline_tag_tags_nothing(fl2):
    client, store, sid = fl2
    out = _fl(client, "create", source_id=sid, group_by=["Host"],
              sort_column="When", columns=[])
    assert "timeline_tag" not in out
    assert store.tag_counts(out["source"]["id"])["counts"] == {}
