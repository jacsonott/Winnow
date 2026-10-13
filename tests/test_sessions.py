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


def test_two_tags_differing_only_in_case_survive_a_session_round_trip(store, write_csv):
    """tag_defs.name is not unique by case, and an analyst can keep
    "Malware" and "malware" apart on purpose. Session load matched by
    name.lower(), so saving and reloading your OWN session moved every row
    of one onto the other — while copy and diff matched exactly, and said
    nothing had changed."""
    sid = store.ingest_csv(write_csv([["a"], ["1"], ["2"], ["3"]]), name="ev.csv", build_fts=False)["id"]
    upper = store.upsert_tag(None, "Malware", "#ff0000", None)["id"]
    lower = store.upsert_tag(None, "malware", "#00ff00", None)["id"]
    store.set_tags(sid, [1], upper, True)
    store.set_tags(sid, [2], lower, True)
    store.save_session("p")
    store.start_new_session()
    store.load_session("p", merge=False)
    names = {t["id"]: t["name"] for t in store.list_tags()}
    got = sorted((r, names[t]) for r, t in store.db.execute(
        "SELECT rid, tag_id FROM row_tags WHERE source_id=?", (sid,)))
    assert got == [(1, "Malware"), (2, "malware")]
    assert store.diff_sessions("p", store.LIVE_SESSION)["counts"]["changed"] == 0


def test_a_tag_differing_only_in_case_pairs_when_that_is_unambiguous(store, write_csv):
    """The other half of the rule: one local tag that differs only in case
    is the same tag (another analyst typed "malware"), so it merges rather
    than duplicates — but not when the session itself carries both
    spellings, which are then two tags on that side too."""
    sid = store.ingest_csv(write_csv([["a"], ["1"], ["2"], ["3"]]), name="ev.csv", build_fts=False)["id"]
    local = store.upsert_tag(None, "Malware", "#ff0000", None)["id"]
    fh = store.get_source(sid)["file_hash"]
    one = {"source": {"file_hash": fh}, "row_notes": [],
           "tag_defs": [{"id": 50, "name": "MALWARE", "color": "#000000", "hotkey": None}],
           "row_tags": [{"rid": 1, "tag_id": 50}]}
    store.import_session(sid, one, merge=True)
    assert [t["name"] for t in store.list_tags()].count("MALWARE") == 0
    assert store.tag_counts(sid)["counts"] == {str(local): 1}

    both = {"source": {"file_hash": fh}, "row_notes": [],
            "tag_defs": [{"id": 60, "name": "malware", "color": "#000000", "hotkey": None},
                         {"id": 61, "name": "MALWARE", "color": "#000000", "hotkey": None}],
            "row_tags": [{"rid": 2, "tag_id": 60}, {"rid": 3, "tag_id": 61}]}
    store.import_session(sid, both, merge=True)
    names = {t["id"]: t["name"] for t in store.list_tags()}
    got = sorted((r, names[t]) for r, t in store.db.execute(
        "SELECT rid, tag_id FROM row_tags WHERE source_id=?", (sid,)))
    assert got == [(1, "Malware"), (2, "malware"), (3, "MALWARE")]


def test_pair_tag_names_is_exact_first_then_unique_case_insensitive():
    from winnow.store import pair_tag_names

    assert pair_tag_names(["Malware", "malware"], ["Malware", "malware"]) == {
        "Malware": "Malware", "malware": "malware"}
    assert pair_tag_names(["MALWARE"], ["Malware"]) == {"MALWARE": "Malware"}
    assert pair_tag_names(["MALWARE"], ["Malware", "malware"]) == {}       # ambiguous locally
    assert pair_tag_names(["MALWARE", "malware"], ["Malware"]) == {}       # ambiguous incoming
    assert pair_tag_names(["Benign"], ["Malware"]) == {}
