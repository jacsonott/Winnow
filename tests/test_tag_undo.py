"""Undo for tag applies/removes.

The load-bearing property throughout is that undo reverses the rows a
change *actually moved*, never the rows it was aimed at. Tagging a
selection that partly overlaps an existing tag, then undoing, must leave
the pre-existing assignments exactly as they were — the difference between
a working undo and one that silently eats an analyst's earlier triage.
"""

from __future__ import annotations

import pytest


def tagged_rids(store, source_id, tag_id):
    return {
        r[0] for r in store.db.execute(
            "SELECT rid FROM row_tags WHERE source_id=? AND tag_id=?", (source_id, tag_id)
        )
    }


def test_undo_apply_restores_previous_state(ingested):
    store, source_id = ingested
    tag = store.upsert_tag(None, "Reviewed", "#00ff00", None)
    store.set_tags(source_id, [1, 2, 3], tag["id"], True)

    before = tagged_rids(store, source_id, tag["id"])
    assert before == {1, 2, 3}

    store.undo_last_tag_change()
    assert tagged_rids(store, source_id, tag["id"]) == set()


def test_undo_does_not_strip_rows_that_were_already_tagged(ingested):
    """The whole point. Rows 1-2 were tagged earlier and are not part of
    what the second change moved, so undoing the second change must leave
    them tagged."""
    store, source_id = ingested
    tag = store.upsert_tag(None, "Reviewed", "#00ff00", None)
    store.set_tags(source_id, [1, 2], tag["id"], True)
    store.set_tags(source_id, [2, 3, 4], tag["id"], True)  # only 3 and 4 move

    store.undo_last_tag_change()
    assert tagged_rids(store, source_id, tag["id"]) == {1, 2}


def test_undo_remove_restores_only_what_was_removed(ingested):
    store, source_id = ingested
    tag = store.upsert_tag(None, "Reviewed", "#00ff00", None)
    store.set_tags(source_id, [1, 2], tag["id"], True)
    store.set_tags(source_id, [2, 3], tag["id"], False)  # only 2 actually moves
    assert tagged_rids(store, source_id, tag["id"]) == {1}

    store.undo_last_tag_change()
    assert tagged_rids(store, source_id, tag["id"]) == {1, 2}  # not 3


def test_undo_is_a_stack(ingested):
    store, source_id = ingested
    tag = store.upsert_tag(None, "Reviewed", "#00ff00", None)
    store.set_tags(source_id, [1], tag["id"], True)
    store.set_tags(source_id, [2], tag["id"], True)
    store.set_tags(source_id, [3], tag["id"], True)

    store.undo_last_tag_change()
    assert tagged_rids(store, source_id, tag["id"]) == {1, 2}
    store.undo_last_tag_change()
    assert tagged_rids(store, source_id, tag["id"]) == {1}
    store.undo_last_tag_change()
    assert tagged_rids(store, source_id, tag["id"]) == set()

    with pytest.raises(ValueError):
        store.undo_last_tag_change()


def test_a_no_op_tag_leaves_nothing_to_undo(ingested):
    """Re-tagging already-tagged rows moves nothing. Recording an entry for
    it would make the next Ctrl+Z a press that visibly does nothing."""
    store, source_id = ingested
    tag = store.upsert_tag(None, "Reviewed", "#00ff00", None)
    store.set_tags(source_id, [1, 2], tag["id"], True)
    depth = store.undo_peek()["depth"]

    store.set_tags(source_id, [1, 2], tag["id"], True)  # no rows move
    assert store.undo_peek()["depth"] == depth

    store.undo_last_tag_change()
    assert tagged_rids(store, source_id, tag["id"]) == set()


def test_undo_peek_describes_the_pending_undo(ingested):
    store, source_id = ingested
    tag = store.upsert_tag(None, "Suspicious", "#ff0000", None)
    assert store.undo_peek()["available"] is False

    store.set_tags(source_id, [1, 2], tag["id"], True)
    peek = store.undo_peek()
    assert peek["available"] is True
    assert peek["count"] == 2
    assert "Suspicious" in peek["label"]
    assert peek["depth"] == 1


def test_undo_of_a_whole_view_tag(ingested):
    store, source_id = ingested
    tag = store.upsert_tag(None, "Bulk", "#123456", None)
    store.set_tags(source_id, [1], tag["id"], True)  # pre-existing

    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    res = store.tag_view(view["view_id"], tag["id"], True)
    assert res["changed"] == view["row_count"] - 1  # row 1 was already tagged

    store.undo_last_tag_change()
    assert tagged_rids(store, source_id, tag["id"]) == {1}


def test_undo_of_a_filtered_view_tag(ingested):
    store, source_id = ingested
    tag = store.upsert_tag(None, "Filtered", "#abcdef", None)
    view = store.build_view(source_id, {
        "source_id": source_id,
        "filters": [{"column": "Process", "op": "contains", "value": "svchost"}],
        "sort": [],
    })
    store.tag_view(view["view_id"], tag["id"], True)
    tagged = tagged_rids(store, source_id, tag["id"])
    assert tagged  # the fixture has at least one svchost row

    store.undo_last_tag_change()
    assert tagged_rids(store, source_id, tag["id"]) == set()


def test_undo_across_merged_sources(store, write_csv):
    p1 = write_csv([["A"], ["1"], ["2"]], name="m1.csv")
    p2 = write_csv([["A"], ["3"]], name="m2.csv")
    rec1 = store.ingest_csv(p1, name="m1.csv", build_fts=False)
    rec2 = store.ingest_csv(p2, name="m2.csv", build_fts=False)
    tag = store.upsert_tag(None, "Cross", "#0000ff", None)

    store.set_tags_pairs([[rec1["id"], 1]], tag["id"], True)  # pre-existing
    store.set_tags_pairs([[rec1["id"], 1], [rec2["id"], 1]], tag["id"], True)

    out = store.undo_last_tag_change()
    assert out["counts"][str(tag["id"])] == 1
    assert tagged_rids(store, rec1["id"], tag["id"]) == {1}
    assert tagged_rids(store, rec2["id"], tag["id"]) == set()


def test_deleting_a_tag_drops_its_undo_history(ingested):
    """The assignments are gone with the definition; an entry that could
    reinsert rows pointing at a dead tag_id has to go with them."""
    store, source_id = ingested
    keep = store.upsert_tag(None, "Keep", "#111111", None)
    doomed = store.upsert_tag(None, "Doomed", "#222222", None)
    store.set_tags(source_id, [1], keep["id"], True)
    store.set_tags(source_id, [2], doomed["id"], True)
    assert store.undo_peek()["depth"] == 2

    store.delete_tag(doomed["id"])
    peek = store.undo_peek()
    assert peek["depth"] == 1
    assert peek["tag_id"] == keep["id"]

    store.undo_last_tag_change()
    assert tagged_rids(store, source_id, keep["id"]) == set()


def test_history_is_bounded(ingested, monkeypatch):
    store, source_id = ingested
    tag = store.upsert_tag(None, "Churn", "#333333", None)
    monkeypatch.setattr(type(store), "UNDO_LIMIT", 3)

    for rid in range(1, 6):
        store.set_tags(source_id, [rid], tag["id"], True)
    assert store.undo_peek()["depth"] == 3

    # The evicted entries' scratch tables go with them.
    live = {r[0] for r in store.db.execute("SELECT name FROM v.sqlite_master WHERE type='table'")}
    assert len([n for n in live if n.startswith("undo_")]) == 3


def test_undo_routes(client, ingested):
    store, source_id = ingested
    tag = client.post("/api/tags", json={"name": "Routed", "color": "#00ffff"}).json()

    assert client.get("/api/row_tags/undo").json()["available"] is False
    # Nothing to undo is a 400 the UI reports as "nothing to undo", not a 500.
    assert client.post("/api/row_tags/undo", json={}).status_code == 400

    client.post("/api/row_tags", json={
        "source_id": source_id, "rids": [1, 2], "tag_id": tag["id"], "on": True,
    })
    peek = client.get("/api/row_tags/undo").json()
    assert peek["available"] is True and peek["count"] == 2

    r = client.post("/api/row_tags/undo", json={})
    assert r.status_code == 200
    body = r.json()
    assert "Routed" in body["undone"]
    assert body["next"]["available"] is False
    assert tagged_rids(store, source_id, tag["id"]) == set()


def test_undo_of_a_tagged_group(ingested):
    """The third tag-write path — a group sub-view, which tags through
    _tag_virtual_group rather than the view table."""
    store, source_id = ingested
    tag = store.upsert_tag(None, "Grouped", "#654321", None)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    groups = store.group_summary(view["view_id"], "User", order="count")["groups"]
    target = groups[0]["value"]
    sub = store.expand_group(view["view_id"], "User", target)

    res = store.tag_view(sub["view_id"], tag["id"], True)
    tagged = tagged_rids(store, source_id, tag["id"])
    assert tagged and res["changed"] == len(tagged)

    store.undo_last_tag_change()
    assert tagged_rids(store, source_id, tag["id"]) == set()


def test_undo_survives_a_view_rebuild(ingested):
    """Undo history and view materialisation share the scratch database;
    evicting a view must not take the delta table with it."""
    store, source_id = ingested
    tag = store.upsert_tag(None, "Rebuilt", "#0abcde", None)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    store.tag_view(view["view_id"], tag["id"], True)

    # Rebuilding evicts the old root view (and drops its backing table).
    store.build_view(source_id, {
        "source_id": source_id,
        "filters": [{"column": "Process", "op": "contains", "value": "e"}],
        "sort": [],
    })

    store.undo_last_tag_change()
    assert tagged_rids(store, source_id, tag["id"]) == set()
