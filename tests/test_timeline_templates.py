"""Timeline template matching (header set and/or file-name wildcard),
the old-shape migration, default seeding, export/import, and the
multi-timestamp (MACB) + search behavior of build_timeline through the
full API path. See CLAUDE.md's unified-Timeline entry."""

from __future__ import annotations

import json

import workspace as WS


def _tpl(**kw):
    base = dict(template_id=None, type_label="T", col_names=None,
                filename_pattern=None, timestamp_columns=[], body_columns=[])
    base.update(kw)
    return WS.timeline_templates.upsert(
        base["template_id"], base["type_label"], base["col_names"],
        base["filename_pattern"], base["timestamp_columns"], base["body_columns"])


def test_matching_precedence_headers_beat_patterns():
    cols = ["TimeCreated", "EventId"]
    _tpl(type_label="by-pattern", filename_pattern="*evtx*")
    h = _tpl(type_label="by-headers", col_names=cols)
    hit = WS.timeline_templates.find_for_source(cols, "my_evtx_output.csv")
    assert hit["type_label"] == "by-headers"
    # Saving again for the same header set updates in place (the old
    # keyed-by-headers dedupe) rather than stacking a second template.
    b = _tpl(type_label="by-both", col_names=cols, filename_pattern="*evtx*")
    assert b["id"] == h["id"]
    # Both-criteria templates out-rank either alone…
    hit = WS.timeline_templates.find_for_source(cols, "my_evtx_output.csv")
    assert hit["type_label"] == "by-both"
    # …and require both: a file the pattern misses falls through to the
    # pattern-only template? No — that one misses too. Nothing matches.
    assert WS.timeline_templates.find_for_source(cols, "other.csv") is None


def test_pattern_matching_is_case_insensitive_wildcard():
    _tpl(type_label="mft", filename_pattern="*MFTECmd*$MFT_Output*")
    hit = WS.timeline_templates.find_for_source(
        ["A"], "20260329222647_mftecmd_$mft_output.csv")
    assert hit and hit["type_label"] == "mft"
    assert WS.timeline_templates.find_for_source(["A"], "evtx.csv") is None or \
        WS.timeline_templates.find_for_source(["A"], "evtx.csv")["type_label"] != "mft"


def test_newest_wins_within_a_tier():
    _tpl(type_label="older", filename_pattern="*log*")
    _tpl(type_label="newer", filename_pattern="*catalog*")
    hit = WS.timeline_templates.find_for_source(["A"], "catalog.csv")
    assert hit["type_label"] == "newer"  # both patterns match; higher id wins


def test_old_shape_records_migrate():
    items = WS.timeline_templates.list()  # forces default seeding first
    WS.timeline_templates._save(items + [{
        "id": 9999, "col_names": ["a", "b"], "type_label": "old",
        "timestamp_column": "a", "body_columns": ["b"],
    }])
    rec = next(t for t in WS.timeline_templates.list() if t["id"] == 9999)
    assert rec["timestamp_columns"] == [{"column": "a", "label": None}]
    assert rec["filename_pattern"] is None


def test_defaults_seed_once():
    first = WS.timeline_templates.list()
    assert len(first) == len(WS.DEFAULT_TIMELINE_TEMPLATES)
    assert any(t["type_label"] == "MFT" for t in first)
    # Deleting one and re-listing must NOT re-seed it.
    WS.timeline_templates.delete(first[0]["id"])
    assert len(WS.timeline_templates.list()) == len(first) - 1


def test_export_import_round_trip():
    _tpl(type_label="mine", filename_pattern="*special*",
         timestamp_columns=[{"column": "TS", "label": "Written"}], body_columns=["X"])
    data = WS.timeline_templates.export_all()
    assert data["format"].startswith("winnow-timeline-templates/")
    # Re-import into the same store: everything is a duplicate, nothing added.
    assert WS.timeline_templates.import_all(json.loads(json.dumps(data))) == 0
    # Wipe and import fresh: everything comes back.
    for t in list(WS.timeline_templates.list()):
        WS.timeline_templates.delete(t["id"])
    added = WS.timeline_templates.import_all(data)
    assert added == len(data["templates"])
    rec = next(t for t in WS.timeline_templates.list() if t["type_label"] == "mine")
    assert rec["timestamp_columns"] == [{"column": "TS", "label": "Written"}]


def test_template_with_no_criteria_is_rejected(client):
    r = client.post("/api/timeline_templates", json={"type_label": "x"})
    assert r.status_code == 400


# ------------------------------------------------- multi-timestamp + search


def _mft_like_source(store, write_csv):
    p = write_csv([
        ["FileName", "Created0x10", "LastModified0x10"],
        ["evil.exe", "2026-01-01 10:00:00", "2026-01-02 11:00:00"],
        ["partial.txt", "2026-01-03 09:00:00", ""],
    ], name="20260329_MFTECmd_$MFT_Output.csv")
    rec = store.ingest_csv(p, name="20260329_MFTECmd_$MFT_Output.csv", build_fts=False)
    store.set_tags(rec["id"], [1, 2], 1, True)
    return rec["id"]


def test_multi_timestamp_one_event_per_clock(client, store, write_csv):
    sid = _mft_like_source(store, write_csv)
    WS.timeline_templates.upsert(None, "MFT-test", None, "*MFTECmd*$MFT*",
                                 [{"column": "Created0x10", "label": None},
                                  {"column": "LastModified0x10", "label": None}],
                                 ["FileName"])
    v = client.post("/api/timeline", json={}).json()
    # 2 rows x 2 clocks, minus partial.txt's empty LastModified = 3 events.
    assert v["row_count"] == 3
    rows = client.get(f"/api/timeline_rows?view_id={v['view_id']}&start=0&count=10").json()["rows"]
    assert [r["ts_label"] for r in rows] == ["Created0x10", "LastModified0x10", "Created0x10"]
    assert all(r["type_label"] == "MFT-test" for r in rows)


def test_single_timestamp_has_no_label_and_keeps_null_rows(client, store, write_csv):
    p = write_csv([
        ["When", "What"],
        ["2026-01-01 10:00:00", "a"],
        ["not a date", "b"],
    ], name="plain.csv")
    rec = store.ingest_csv(p, name="plain.csv", build_fts=False)
    store.set_tags(rec["id"], [1, 2], 1, True)
    v = client.post("/api/timeline", json={}).json()
    assert v["row_count"] == 2  # the unparseable-ts row still shows, sorted last
    rows = client.get(f"/api/timeline_rows?view_id={v['view_id']}&start=0&count=10").json()["rows"]
    assert all(r["ts_label"] is None for r in rows)
    assert rows[-1]["ts"] is None


def test_timeline_search_filters_derived_events(client, store, write_csv):
    sid = _mft_like_source(store, write_csv)
    WS.timeline_templates.upsert(None, "MFT-test", None, "*MFTECmd*$MFT*",
                                 [{"column": "Created0x10", "label": None}],
                                 ["FileName"])
    assert client.post("/api/timeline", json={"search": "evil"}).json()["row_count"] == 1
    assert client.post("/api/timeline", json={"search": "MFT-test"}).json()["row_count"] == 2
    assert client.post("/api/timeline", json={"search": "zzz-none"}).json()["row_count"] == 0
    # LIKE metacharacters in the search are literals, not wildcards.
    assert client.post("/api/timeline", json={"search": "%"}).json()["row_count"] == 0


def test_kape_defaults_place_real_headers(client, store, write_csv):
    """The shipped defaults against a synthetic file shaped like the sample
    triage's $MFT output: matched by name, MACB expansion applies."""
    p = write_csv([
        ["EntryNumber", "ParentPath", "FileName", "Extension", "FileSize",
         "Created0x10", "LastModified0x10", "LastRecordChange0x10", "LastAccess0x10",
         "ZoneIdContents"],
        ["42", ".\\Users", "mal.exe", ".exe", "100",
         "2026-01-01 10:00:00", "2026-01-01 10:05:00", "2026-01-01 10:05:00", "2026-01-02 08:00:00",
         ""],
    ], name="20260329222647_MFTECmd_$MFT_Output.csv")
    rec = store.ingest_csv(p, name="20260329222647_MFTECmd_$MFT_Output.csv", build_fts=False)
    store.set_tags(rec["id"], [1], 1, True)
    v = client.post("/api/timeline", json={}).json()
    assert v["row_count"] == 4  # one event per MACB clock
    rows = client.get(f"/api/timeline_rows?view_id={v['view_id']}&start=0&count=10").json()["rows"]
    assert {r["ts_label"] for r in rows} == {
        "Created0x10", "LastModified0x10", "LastRecordChange0x10", "LastAccess0x10"}
    assert all(r["type_label"] == "MFT" for r in rows)
    assert all("mal.exe" in r["body"] for r in rows)


# ---------------------------------------- sort, body joining, row fetch


def test_sort_desc_reverses_but_null_ts_stays_last(client, store, write_csv):
    p = write_csv([
        ["When", "What"],
        ["2026-01-01 10:00:00", "early"],
        ["2026-01-05 10:00:00", "late"],
        ["not a date", "dateless"],
    ], name="plain.csv")
    rec = store.ingest_csv(p, name="plain.csv", build_fts=False)
    store.set_tags(rec["id"], [1, 2, 3], 1, True)

    v = client.post("/api/timeline", json={"sort": "desc"}).json()
    rows = client.get(f"/api/timeline_rows?view_id={v['view_id']}&start=0&count=10").json()["rows"]
    assert [r["body"].split(" | ")[-1] for r in rows] == ["late", "early", "dateless"]
    assert rows[-1]["ts"] is None  # unplaceable rows sort last in both directions

    v = client.post("/api/timeline", json={"sort": "asc"}).json()
    rows = client.get(f"/api/timeline_rows?view_id={v['view_id']}&start=0&count=10").json()["rows"]
    assert [r["body"].split(" | ")[-1] for r in rows] == ["early", "late", "dateless"]


def test_body_join_skips_empty_cells(client, store, write_csv):
    p = write_csv([
        ["When", "A", "B", "C"],
        ["2026-01-01 10:00:00", "left", "", "right"],
        ["2026-01-02 10:00:00", "", "", "only"],
    ], name="sparse.csv")
    rec = store.ingest_csv(p, name="sparse.csv", build_fts=False)
    store.set_tags(rec["id"], [1, 2], 1, True)
    WS.timeline_templates.upsert(None, "sparse", None, "sparse*",
                                 [{"column": "When", "label": None}], ["A", "B", "C"])
    v = client.post("/api/timeline", json={}).json()
    rows = client.get(f"/api/timeline_rows?view_id={v['view_id']}&start=0&count=10").json()["rows"]
    assert rows[0]["body"] == "left | right"   # no dangling separator for empty B
    assert rows[1]["body"] == "only"


def test_body_join_expr_chunks_wide_sources(client, store, write_csv):
    from store import _body_join_expr
    # The expr builder nests past SQLite's per-call argument cap...
    expr = _body_join_expr([f'"c{i}"' for i in range(200)])
    assert expr.count("BODY_JOIN(") == 4  # 90 + 90 + 20 inner chunks under one outer call
    # ...and a genuinely wide source builds end to end (fallback body =
    # every column).
    header = ["When"] + [f"col{i}" for i in range(119)]
    row = ["2026-01-01 10:00:00"] + [f"v{i}" if i % 7 == 0 else "" for i in range(119)]
    p = write_csv([header, row], name="wide.csv")
    rec = store.ingest_csv(p, name="wide.csv", build_fts=False)
    store.set_tags(rec["id"], [1], 1, True)
    v = client.post("/api/timeline", json={}).json()
    assert v["row_count"] == 1
    rows = client.get(f"/api/timeline_rows?view_id={v['view_id']}&start=0&count=10").json()["rows"]
    assert "v0 | v7" in rows[0]["body"] and " |  | " not in rows[0]["body"]


def test_api_row_returns_full_source_row(client, store, write_csv):
    p = write_csv([
        ["When", "What"],
        ["2026-01-01 10:00:00", "hello"],
    ], name="plain.csv")
    rec = store.ingest_csv(p, name="plain.csv", build_fts=False)
    store.set_tags(rec["id"], [1], 2, True)
    store.set_note(rec["id"], 1, "interesting")

    r = client.get(f"/api/row?source_id={rec['id']}&rid=1")
    assert r.status_code == 200
    d = r.json()
    assert d["source_name"] == "plain.csv"
    assert [c["name"] for c in d["columns"]] == ["When", "What"]
    assert d["cells"] == ["2026-01-01 10:00:00", "hello"]
    assert d["tags"] == [2]
    assert d["note"] == "interesting"

    assert client.get(f"/api/row?source_id={rec['id']}&rid=999").status_code == 404
