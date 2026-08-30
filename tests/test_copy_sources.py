"""Copying sources between case files — the ATTACH-copy primitive behind
"save these quick-look tables into my real case".

The assertions that matter: rids survive byte-for-byte (they are what tags
and notes point at), tags land by NAME in a differently-numbered target,
and a target open in another Winnow is refused with the holder named."""

from __future__ import annotations

import json

import pytest

from winnow.store import Store

ROWS = [["Host", "User"], ["h1", "alice"], ["h2", "bob"], ["h3", "carol"]]


def _fill(store, write_csv, name="quick.csv"):
    return store.ingest_csv(write_csv(ROWS, name), name=name, build_fts=False)["id"]


def _tag(store, sid, rids, tag_name, color="#ff0000", hotkey=None):
    tag = next((t for t in store.list_tags() if t["name"] == tag_name), None)
    if tag is None:
        tag = store.upsert_tag(None, tag_name, color, hotkey)
    store.set_tags(sid, list(rids), tag["id"], True)
    return tag["id"]


@pytest.fixture
def target(tmp_path):
    t = Store(str(tmp_path / "real-case.db"))
    yield t
    t.close()


def test_rows_tags_notes_and_rids_survive_the_copy(store, write_csv, target):
    sid = _fill(store, write_csv)
    _tag(store, sid, [1, 3], "Lateral movement")
    store.set_note(sid, 3, "the pivot host")
    target_path = target.path
    target.close()   # the primitive opens it itself; a live holder is refused

    res = store.copy_sources_to(target_path, [sid])

    assert len(res["copied"]) == 1
    new_id = res["copied"][0]["id"]
    t = Store(target_path)
    try:
        rows = t.db.execute(f"SELECT rid, Host, User FROM src_{new_id} ORDER BY rid").fetchall()
        assert [tuple(r) for r in rows] == [(1, "h1", "alice"), (2, "h2", "bob"), (3, "h3", "carol")]
        tagged = {r["rid"] for r in t.db.execute(
            "SELECT rid FROM row_tags WHERE source_id=?", (new_id,))}
        assert tagged == {1, 3}, "tags point at the SAME rids they did at home"
        note = t.db.execute("SELECT note FROM row_notes WHERE source_id=? AND rid=3",
                            (new_id,)).fetchone()
        assert note["note"] == "the pivot host"
        src = t.get_source(new_id)
        assert src["row_count"] == 3
        assert src["has_fts"] == 0, "FTS rebuilds in the target, never copies"
    finally:
        t.close()


def test_tags_land_by_name_in_a_differently_numbered_case(store, write_csv, target):
    """The QC rule again: two cases number their tags independently, and a
    copy that matched ids would attach the analyst's findings to whatever
    tag happened to hold that id in the target."""
    sid = _fill(store, write_csv)
    _tag(store, sid, [2], "Beaconing", color="#123456")

    # The target already has tags, differently numbered, including a name
    # collision and several fillers pushing ids around.
    for filler in ("Noise", "Later"):
        target.upsert_tag(None, filler, "#888888", None)
    theirs = target.upsert_tag(None, "Beaconing", "#654321", None)
    target_path = target.path
    target.close()

    res = store.copy_sources_to(target_path, [sid])

    t = Store(target_path)
    try:
        new_id = res["copied"][0]["id"]
        got = t.db.execute("SELECT tag_id FROM row_tags WHERE source_id=?", (new_id,)).fetchone()
        assert got["tag_id"] == theirs["id"], "matched the target's own 'Beaconing', by name"
        # The existing definition kept ITS colour — a copy must not restyle
        # the target's tags.
        beacon = next(x for x in t.list_tags() if x["name"] == "Beaconing")
        assert beacon["color"] == "#654321"
    finally:
        t.close()


def test_missing_tags_are_created_with_their_look(store, write_csv, target):
    sid = _fill(store, write_csv)
    _tag(store, sid, [1], "Exfil", color="#00ff88", hotkey="7")
    target_path = target.path
    target.close()

    store.copy_sources_to(target_path, [sid])

    t = Store(target_path)
    try:
        exfil = next(x for x in t.list_tags() if x["name"] == "Exfil")
        assert exfil["color"] == "#00ff88"
        assert exfil["hotkey"] == "7"
    finally:
        t.close()


def test_derived_columns_come_along_with_their_values(store, write_csv, target):
    sid = _fill(store, write_csv)
    res = store.add_derived_column(sid, "HostUpper", "Host", "regex_extract", {"pattern": "(h.)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    target_path = target.path
    target.close()

    out = store.copy_sources_to(target_path, [sid])
    new_id = out["copied"][0]["id"]

    t = Store(target_path)
    try:
        defs = t.list_derived_columns(new_id)
        assert [d["name"] for d in defs] == ["HostUpper"]
        assert defs[0]["status"] == "ready", "values were copied, not queued for recompute"
        vals = t.db.execute(f'SELECT rid, "HostUpper" FROM drv_{new_id} ORDER BY rid').fetchall()
        assert [tuple(v) for v in vals][:2] == [(1, "h1"), (2, "h2")]
    finally:
        t.close()


def test_a_target_open_in_another_winnow_is_refused_by_name(store, write_csv, target):
    """Writing into a case another server has open is exactly what the case
    lock exists to prevent — the refusal has to say who has it."""
    sid = _fill(store, write_csv)
    # `target` is still open — its lock is held, like a second Winnow's would be.
    with pytest.raises(ValueError, match="open in another Winnow"):
        store.copy_sources_to(target.path, [sid])
    # And nothing was written into it.
    assert target.list_sources() == []


def test_copying_into_yourself_is_refused(store, write_csv):
    sid = _fill(store, write_csv)
    with pytest.raises(ValueError, match="this case"):
        store.copy_sources_to(store.path, [sid])


def test_merges_are_refused_with_a_pointer(store, write_csv, target):
    a = _fill(store, write_csv, "a.csv")
    b = _fill(store, write_csv, "b.csv")
    merge = store.create_merge("m", [a, b])
    assert merge["id"] < 0, "create_merge returns the SIGNED id — the fixture must not re-negate it"
    target_path = target.path
    target.close()
    with pytest.raises(ValueError, match="member tables"):
        store.copy_sources_to(target_path, [merge["id"]])


def test_an_old_schema_target_is_migrated_before_the_write(store, write_csv, tmp_path):
    """A case created by an older Winnow may lack newer tables; opening the
    target as a full Store is what runs its migrations first."""
    import sqlite3
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript("""
      CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT NOT NULL, path TEXT,
        table_name TEXT NOT NULL, row_count INTEGER NOT NULL DEFAULT 0,
        columns TEXT NOT NULL, file_hash TEXT, imported_at TEXT,
        has_fts INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE tag_defs (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
        color TEXT NOT NULL, hotkey TEXT);
    """)
    conn.commit()
    conn.close()

    sid = _fill(store, write_csv)
    res = store.copy_sources_to(str(old), [sid])
    assert res["copied"][0]["rows"] == 3

    t = Store(str(old))
    try:
        assert [s["name"] for s in t.list_sources()] == ["quick.csv"]
    finally:
        t.close()


def test_the_source_case_is_untouched(store, write_csv, target):
    """Reads only, through the attachment — the quick-look case the analyst
    copied FROM still has everything afterwards."""
    sid = _fill(store, write_csv)
    _tag(store, sid, [1], "Keep")
    before = store.db.execute(f"SELECT COUNT(*) FROM src_{sid}").fetchone()[0]
    target_path = target.path
    target.close()

    store.copy_sources_to(target_path, [sid])

    assert store.db.execute(f"SELECT COUNT(*) FROM src_{sid}").fetchone()[0] == before
    assert store.db.execute("SELECT COUNT(*) FROM row_tags WHERE source_id=?",
                            (sid,)).fetchone()[0] == 1
    assert store.get_source(sid)["name"] == "quick.csv"


# --------------------------------------------------------------- HTTP layer

def test_the_route_maps_refusals_to_the_right_codes(store, write_csv, target, monkeypatch):
    import server
    from fastapi.testclient import TestClient
    monkeypatch.setattr(server, "STORE", store)
    client = TestClient(server.app)
    headers = {"X-Timeline-Lite-Client": "1"}
    sid = _fill(store, write_csv)

    # Locked target → 409, the same contract as opening a locked case.
    res = client.post("/api/case/copy_sources", headers=headers,
                      json={"target_path": target.path, "source_ids": [sid]})
    assert res.status_code == 409

    target_path = target.path
    target.close()
    res = client.post("/api/case/copy_sources", headers=headers,
                      json={"target_path": target_path, "source_ids": [sid]})
    assert res.status_code == 200
    assert res.json()["copied"][0]["rows"] == 3

    res = client.post("/api/case/copy_sources", headers=headers,
                      json={"target_path": target_path, "source_ids": [999]})
    assert res.status_code == 404


def test_copy_refuses_while_an_import_runs(store, write_csv, target, monkeypatch):
    """A source mid-ingest has an accurate-but-growing row_count; the
    ATTACH copy would snapshot whatever was committed and file it in the
    target as a complete table."""
    import server
    from fastapi.testclient import TestClient
    monkeypatch.setattr(server, "STORE", store)
    monkeypatch.setattr(server, "_jobs_running", lambda: True)
    client = TestClient(server.app)
    sid = _fill(store, write_csv)
    target_path = target.path
    target.close()
    r = client.post("/api/case/copy_sources", headers={"X-Timeline-Lite-Client": "1"},
                    json={"target_path": target_path, "source_ids": [sid]})
    assert r.status_code == 409
    assert "importing" in r.json()["detail"].lower()
