"""Importing another case's watchlist: read-only against the other .db,
deduped by value, kind/note carried, auto-tags deliberately dropped, and
the picker route lists only cases that have indicators to offer."""

from __future__ import annotations

import sqlite3

import pytest

from winnow import workspace as WS
from winnow.store import DEFAULT_TAGS, Store


@pytest.fixture
def other_case(tmp_path):
    """A second, closed case file holding three indicators (one with an
    auto-tag that must not carry)."""
    path = tmp_path / "previous.db"
    other = Store(str(path), default_tags=DEFAULT_TAGS)
    try:
        tag = other.upsert_tag(None, "C2", "#ff0000", None)
        other.add_indicator("185.220.101.4", "ip", "C2 from TI report", tag["id"])
        other.add_indicator("rclone.exe", "filename", None, None)
        other.add_indicator("badstuff.example.com", "domain", None, None)
    finally:
        other.close()
    return str(path)


def test_import_copies_dedupes_and_drops_auto_tags(store, other_case):
    store.add_indicator("rclone.exe", "filename", None, None)   # pre-existing → skipped
    res = store.import_watchlist_from_case(other_case)
    assert res == {"added": 2, "skipped": 1}
    by_value = {i["value"]: i for i in store.list_indicators()}
    assert by_value["185.220.101.4"]["kind"] == "ip"
    assert by_value["185.220.101.4"]["note"] == "C2 from TI report"
    assert by_value["185.220.101.4"]["auto_tag_id"] is None     # other case's tag id
    assert by_value["badstuff.example.com"]["kind"] == "domain"
    # Idempotent: a second import adds nothing.
    assert store.import_watchlist_from_case(other_case)["added"] == 0


def test_own_case_and_pre_watchlist_files_are_handled(store, tmp_path):
    with pytest.raises(ValueError, match="currently open case"):
        store.import_watchlist_from_case(store.path)
    old = tmp_path / "ancient.db"
    sqlite3.connect(old).executescript("CREATE TABLE sources (id INTEGER PRIMARY KEY);")
    assert store.import_watchlist_from_case(str(old)) == {"added": 0, "skipped": 0}


def test_routes_list_and_import(client, store, other_case, tmp_path):
    rec = WS.cases.create(other_case, "Previous engagement")
    # A registered case with no indicators must not be offered.
    empty_path = tmp_path / "empty.db"
    empty = Store(str(empty_path), default_tags=DEFAULT_TAGS)
    empty.close()
    WS.cases.create(str(empty_path), "Empty case")

    r = client.get("/api/watchlist/cases")
    assert r.status_code == 200
    offered = r.json()
    assert [c["name"] for c in offered] == ["Previous engagement"]
    assert offered[0]["indicator_count"] == 3

    r = client.post("/api/watchlist/import_case", json={"case_id": rec["id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 3
    assert len(body["indicators"]) == 3

    r = client.post("/api/watchlist/import_case", json={"case_id": 99999})
    assert r.status_code == 400
