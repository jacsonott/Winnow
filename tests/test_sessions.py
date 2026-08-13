"""store.py sessions: per-source export/import (tag remap by name), named
session save/list/load/delete, and case-level session export/import."""

from __future__ import annotations

import pytest


def test_export_import_session_round_trip(ingested):
    store, source_id = ingested
    tag = store.upsert_tag(None, "Suspicious", "#ff0000", "1")
    store.set_tags(source_id, [1, 2], tag["id"], True)
    store.set_note(source_id, 1, "hello")

    session = store.export_session(source_id)
    assert session["format"] == "winnow-session/1"
    assert len(session["row_tags"]) == 2
    assert session["row_notes"] == [{"rid": 1, "note": "hello"}]

    store.set_tags(source_id, [1, 2], tag["id"], False)  # clear, then re-import the exported session
    res = store.import_session(source_id, session, merge=True)
    assert res["tags_applied"] == 2
    assert res["warnings"] == []
    assert store.tag_counts(source_id)["counts"][str(tag["id"])] == 2


def test_import_session_merges_same_named_tag_instead_of_duplicating(store, write_csv):
    # CLAUDE.md: "Session import remaps tag IDs by name, creating missing
    # tags. Two analysts with their own 'Lateral movement' tag merge rather
    # than duplicate."
    path = write_csv([["A"], ["1"], ["2"]])
    rec = store.ingest_csv(path, name="a.csv", build_fts=False)
    local_tag = store.upsert_tag(None, "Lateral movement", "#111111", None)
    store.set_tags(rec["id"], [1], local_tag["id"], True)

    # A "session" from another analyst with a differently-numbered but
    # same-named tag.
    foreign_session = {
        "source": {"file_hash": store.get_source(rec["id"])["file_hash"]},
        "tag_defs": [{"id": 999, "name": "Lateral movement", "color": "#222222", "hotkey": None}],
        "row_tags": [{"rid": 2, "tag_id": 999}],
        "row_notes": [],
    }
    res = store.import_session(rec["id"], foreign_session, merge=True)
    assert res["warnings"] == []
    tags_after = store.list_tags()
    matching = [t for t in tags_after if t["name"] == "Lateral movement"]
    assert len(matching) == 1  # merged onto the existing tag, not duplicated
    assert store.tag_counts(rec["id"])["counts"][str(local_tag["id"])] == 2  # both rows now


def test_import_session_creates_missing_tag_by_name(store, write_csv):
    path = write_csv([["A"], ["1"]])
    rec = store.ingest_csv(path, name="a.csv", build_fts=False)
    session = {
        "source": {"file_hash": store.get_source(rec["id"])["file_hash"]},
        "tag_defs": [{"id": 1, "name": "Brand New Tag", "color": "#333333", "hotkey": None}],
        "row_tags": [{"rid": 1, "tag_id": 1}],
        "row_notes": [],
    }
    store.import_session(rec["id"], session, merge=True)
    assert "Brand New Tag" in {t["name"] for t in store.list_tags()}


def test_import_session_warns_on_file_hash_mismatch(ingested):
    store, source_id = ingested
    session = {"source": {"file_hash": "not-the-real-hash"}, "tag_defs": [], "row_tags": [], "row_notes": []}
    res = store.import_session(source_id, session)
    assert len(res["warnings"]) == 1
    assert "different file" in res["warnings"][0]


def test_named_session_save_list_load_delete(ingested):
    store, source_id = ingested
    store.upsert_tag(None, "T", "#000000", None)
    saved = store.save_named_session("nightly-run")
    assert saved["name"] == "nightly-run"
    assert saved["source_count"] == 1

    listed = store.list_named_sessions()
    assert any(s["name"] == "nightly-run" for s in listed)

    result = store.load_named_session("nightly-run")
    assert result["sources_restored"] == 1
    assert result["sources_reimported"] == 0  # source is already open, matched by file_hash

    store.delete_named_session("nightly-run")
    assert not any(s["name"] == "nightly-run" for s in store.list_named_sessions())


def test_load_named_session_missing_raises(store):
    with pytest.raises(KeyError):
        store.load_named_session("does-not-exist")


def test_case_session_export_covers_every_open_source(store, write_csv):
    p1 = write_csv([["A"], ["1"]], name="c1.csv")
    p2 = write_csv([["A"], ["2"]], name="c2.csv")
    store.ingest_csv(p1, name="c1.csv", build_fts=False)
    store.ingest_csv(p2, name="c2.csv", build_fts=False)
    case_session = store.export_case_session()
    assert case_session["format"] == "winnow-case-session/1"
    assert len(case_session["sources"]) == 2
