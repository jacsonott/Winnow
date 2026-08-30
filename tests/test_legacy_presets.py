"""The filter_presets one-time migration: a case file from before saved
filters moved to the workspace still carries its own presets table, and
the first open drains it into workspace filters — once, without loss,
and without ever running again."""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from winnow import workspace as WS
from winnow.store import Store

HEADERS = {"X-Timeline-Lite-Client": "1"}

LEGACY = {"name": "Old triage", "col_names": ["When", "Who"],
          "payload": {"filter_tree": {"type": "group", "op": "and", "children": []}}}


def _plant_legacy_preset(case_path: str) -> None:
    """What an old install would have left behind — written with plain
    sqlite so the test doesn't depend on any current-store write path."""
    db = sqlite3.connect(case_path)
    db.execute(
        "INSERT INTO filter_presets(name, col_sig, col_names, payload, created_at)"
        " VALUES (?,?,?,?,?)",
        (LEGACY["name"], "sig", json.dumps(LEGACY["col_names"]),
         json.dumps(LEGACY["payload"]), "2025-01-01T00:00:00"))
    db.commit()
    db.close()


def test_pop_returns_once_and_clears(store):
    _plant_legacy_preset(store.path)
    popped = store.pop_legacy_presets()
    assert [p["name"] for p in popped] == ["Old triage"]
    assert popped[0]["payload"] == LEGACY["payload"]
    assert popped[0]["col_names"] == LEGACY["col_names"]
    # One-time: the second call finds the table already drained.
    assert store.pop_legacy_presets() == []


def test_a_new_scheme_case_pops_nothing(store):
    assert store.pop_legacy_presets() == []


def test_case_open_folds_legacy_presets_into_workspace_filters(tmp_path, monkeypatch):
    """The route-level contract: opening an old case file makes its
    presets appear as ordinary workspace saved filters."""
    import server
    case = tmp_path / "old.db"
    Store(str(case)).close()          # create + migrate the schema
    _plant_legacy_preset(str(case))

    monkeypatch.setattr(server, "STORE", None)
    c = TestClient(server.app)
    res = c.post("/api/case/open", json={"path": str(case)}, headers=HEADERS)
    assert res.status_code == 200
    try:
        names = [f["name"] for f in WS.filters.list()]
        assert "Old triage" in names
        # And the case file itself is drained — reopening won't double-add.
        assert server.STORE.pop_legacy_presets() == []
    finally:
        server.STORE.close()
        server.STORE = None
