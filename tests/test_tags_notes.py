"""store.py tags and notes: CRUD, set_tags vs set_tags_pairs (merged-view
mixed-source tagging), tag_view on a normal view, and note set/clear."""

from __future__ import annotations


def test_upsert_and_delete_tag(store):
    created = store.upsert_tag(None, "Suspicious", "#ff0000", "1")
    assert created["name"] == "Suspicious"
    assert created["id"] is not None

    renamed = store.upsert_tag(created["id"], "Very Suspicious", "#ff0000", "1")
    assert renamed["id"] == created["id"]
    assert renamed["name"] == "Very Suspicious"
    assert len([t for t in store.list_tags() if t["id"] == created["id"]]) == 1

    store.delete_tag(created["id"])
    assert created["id"] not in {t["id"] for t in store.list_tags()}


def test_set_tags_on_and_off(ingested):
    store, source_id = ingested
    tag = store.upsert_tag(None, "Reviewed", "#00ff00", None)
    counts = store.set_tags(source_id, [1, 2], tag["id"], True)
    assert counts["counts"][str(tag["id"])] == 2

    counts = store.set_tags(source_id, [1], tag["id"], False)
    assert counts["counts"].get(str(tag["id"]), 0) == 1


def test_set_tags_pairs_mixed_sources(store, write_csv):
    p1 = write_csv([["A"], ["1"], ["2"]], name="p1.csv")
    p2 = write_csv([["A"], ["3"]], name="p2.csv")
    rec1 = store.ingest_csv(p1, name="p1.csv", build_fts=False)
    rec2 = store.ingest_csv(p2, name="p2.csv", build_fts=False)
    tag = store.upsert_tag(None, "Cross", "#0000ff", None)

    pairs = [[rec1["id"], 1], [rec2["id"], 1]]
    result = store.set_tags_pairs(pairs, tag["id"], True)
    assert result["counts"][str(tag["id"])] == 2

    assert store.set_tags_pairs([], tag["id"], True) == {"counts": {}}


def test_tag_view_tags_every_row_in_the_view(ingested):
    store, source_id = ingested
    tag = store.upsert_tag(None, "AllInView", "#123456", None)
    spec = {"source_id": source_id, "filters": [{"column": "Process", "op": "equals", "value": "cmd.exe"}], "sort": []}
    view = store.build_view(source_id, spec)
    store.tag_view(view["view_id"], tag["id"], True)
    counts = store.tag_counts(source_id)
    assert counts["counts"][str(tag["id"])] == 1  # only the one cmd.exe row, not the whole source

    store.tag_view(view["view_id"], tag["id"], False)
    counts = store.tag_counts(source_id)
    assert counts["counts"].get(str(tag["id"]), 0) == 0


def _tagged_rids(store, source_id, tag_id):
    return {
        r[0] for r in store.db.execute(
            "SELECT rid FROM row_tags WHERE source_id=? AND tag_id=?", (source_id, tag_id)
        )
    }


def test_tag_view_skips_excluded_rows(ingested):
    """"Select all, uncheck a couple, tag" — the frontend models that as a
    flag plus an exclusion set, and hands the exclusions here rather than
    materialising millions of rids it hasn't even fetched."""
    store, source_id = ingested
    tag = store.upsert_tag(None, "MostOfIt", "#123456", None)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    result = store.tag_view(view["view_id"], tag["id"], True, exclude=[[source_id, 2], [source_id, 4]])
    assert _tagged_rids(store, source_id, tag["id"]) == {1, 3}
    assert result["affected"] == 2


def test_tag_view_exclusion_does_not_strip_a_tag_the_row_already_had(ingested):
    """The reason exclusions are a server-side concept at all: tagging
    everything and then untagging the exclusions would clear a tag an
    excluded row legitimately already carried."""
    store, source_id = ingested
    tag = store.upsert_tag(None, "Keep", "#123456", None)
    store.set_tags(source_id, [2], tag["id"], True)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    store.tag_view(view["view_id"], tag["id"], True, exclude=[[source_id, 2]])
    assert _tagged_rids(store, source_id, tag["id"]) == {1, 2, 3, 4}


def test_tag_view_exclusion_on_untag(ingested):
    store, source_id = ingested
    tag = store.upsert_tag(None, "Bulk", "#123456", None)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    store.tag_view(view["view_id"], tag["id"], True)
    store.tag_view(view["view_id"], tag["id"], False, exclude=[[source_id, 3]])
    assert _tagged_rids(store, source_id, tag["id"]) == {3}


def test_tag_view_exclusion_across_a_merge(store, write_csv):
    rows_a = [["Process"], ["a1.exe"], ["a2.exe"]]
    rows_b = [["Process"], ["b1.exe"], ["b2.exe"]]
    a = store.ingest_csv(write_csv(rows_a, "ma.csv"), name="ma", build_fts=False)
    b = store.ingest_csv(write_csv(rows_b, "mb.csv"), name="mb", build_fts=False)
    merge = store.create_merge("m", [a["id"], b["id"]])
    tag = store.upsert_tag(None, "Merged", "#123456", None)
    view = store.build_view(merge["id"], {"source_id": merge["id"], "filters": [], "sort": []})
    # Exclusions carry their row's own real source_id, never the merge's
    # synthetic negative one.
    store.tag_view(view["view_id"], tag["id"], True, exclude=[[b["id"], 1]])
    assert _tagged_rids(store, a["id"], tag["id"]) == {1, 2}
    assert _tagged_rids(store, b["id"], tag["id"]) == {2}


def test_tag_view_exclusion_on_a_virtual_group(store, write_csv):
    rows = [["Process", "User"]] + [["svchost.exe", f"u{i}"] for i in range(5)]
    rec = store.ingest_csv(write_csv(rows, "vg.csv"), name="vg", build_fts=False)
    source_id = rec["id"]
    tag = store.upsert_tag(None, "Group", "#123456", None)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    grp = store.expand_group(view["view_id"], "Process", "svchost.exe")
    store.tag_view(grp["view_id"], tag["id"], True, exclude=[[source_id, 1], [source_id, 5]])
    assert _tagged_rids(store, source_id, tag["id"]) == {2, 3, 4}


def test_note_set_and_clear(ingested):
    store, source_id = ingested
    store.set_note(source_id, 1, "Worth a closer look")
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})
    rows = {r["rid"]: r for r in store.fetch_rows(view["view_id"], 0, 10)["rows"]}
    assert rows[1]["note"] == "Worth a closer look"
    assert rows[2]["note"] is None

    store.set_note(source_id, 1, "   ")  # blank/whitespace clears it
    rows = {r["rid"]: r for r in store.fetch_rows(view["view_id"], 0, 10)["rows"]}
    assert rows[1]["note"] is None
