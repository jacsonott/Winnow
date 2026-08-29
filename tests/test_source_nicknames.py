"""Table nicknames: a display name over the imported file's name.

The contract under test — `name` is never rewritten (it's the file's
identity: session hash warnings, the record of what was imported), the
nickname rides alongside and clears back to nothing; a merge has no file
behind it, so nicknaming one is a rename of the merge itself; and a case
file from before the column existed is patched on open."""

from __future__ import annotations

import sqlite3

import pytest

from winnow.store import DEFAULT_TAGS, Store


def test_nickname_set_and_clear(ingested):
    store, sid = ingested
    rec = store.set_source_nickname(sid, "DC01 security log")
    assert rec["nickname"] == "DC01 security log"
    assert rec["name"] == "standard.csv"  # identity untouched

    assert store.get_source(sid)["nickname"] == "DC01 security log"
    assert any(s["nickname"] == "DC01 security log" for s in store.list_sources())

    # Empty (or whitespace) clears it rather than storing "".
    rec = store.set_source_nickname(sid, "   ")
    assert rec["nickname"] is None


def test_nickname_on_merge_renames_it(store, write_csv):
    rows = [["A", "B"], ["1", "2"]]
    a = store.ingest_csv(write_csv(rows, "a.csv"), name="a.csv")["id"]
    b = store.ingest_csv(write_csv(rows, "b.csv"), name="b.csv")["id"]
    merge = store.create_merge("first name", [a, b])

    rec = store.set_source_nickname(merge["id"], "both logs")
    assert rec["name"] == "both logs"

    # A merge's name is its display name — clearing makes no sense there.
    try:
        store.set_source_nickname(merge["id"], "")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_nickname_unknown_source_is_keyerror(store):
    try:
        store.set_source_nickname(999, "x")
        assert False, "expected KeyError"
    except KeyError:
        pass


@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 35, 0),
                    reason="simulating the old schema needs DROP COLUMN")
def test_nickname_column_added_to_old_case_file(tmp_path, write_csv):
    """A case file created before the nickname column existed opens fine
    and can be nicknamed — the ALTER TABLE patch in Store.__init__."""
    path = str(tmp_path / "old.db")
    s = Store(path, default_tags=DEFAULT_TAGS)
    sid = s.ingest_csv(write_csv([["A"], ["1"]], "old.csv"), name="old.csv")["id"]
    s.close()

    # Simulate the pre-nickname schema by dropping the column outright.
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE sources DROP COLUMN nickname")
    db.commit()
    db.close()

    s = Store(path, default_tags=DEFAULT_TAGS)
    try:
        assert s.get_source(sid)["nickname"] is None
        assert s.set_source_nickname(sid, "patched")["nickname"] == "patched"
    finally:
        s.close()


def test_nickname_route(client, ingested):
    _, sid = ingested
    r = client.post(f"/api/source/{sid}/nickname", json={"nickname": "evtx"})
    assert r.status_code == 200
    assert r.json()["nickname"] == "evtx"

    r = client.post(f"/api/source/{sid}/nickname", json={"nickname": ""})
    assert r.status_code == 200
    assert r.json()["nickname"] is None

    r = client.post("/api/source/999/nickname", json={"nickname": "x"})
    assert r.status_code == 404

    r = client.post(f"/api/source/{sid}/nickname", json={"nickname": "x" * 300})
    assert r.status_code == 400
