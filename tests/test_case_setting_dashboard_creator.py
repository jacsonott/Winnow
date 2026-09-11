"""Case settings → Dashboard creator mode: a per-case opt-in stored in
case_settings, and a save route that only writes the keys it was sent."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server
from winnow.store import Store


@pytest.fixture
def client(tmp_path):
    store = Store(str(tmp_path / "case.db-winnow"))
    server.STORE = store
    server.ALLOWED_HOSTS = server.ALLOWED_HOSTS | {"testserver"}
    try:
        yield TestClient(server.app, headers={"X-Timeline-Lite-Client": "1"})
    finally:
        store.close()


def test_on_is_stored_as_1_and_off_removes_the_key(client):
    assert client.get("/api/case_settings").json() == {}
    assert client.post("/api/case_settings", json={"dashboard_creator": True}).json() == {"dashboard_creator": "1"}
    assert client.get("/api/case_settings").json()["dashboard_creator"] == "1"
    assert client.post("/api/case_settings", json={"dashboard_creator": False}).json() == {}
    # Absent means off — no "0" left behind to confuse a `=== '1'` check.
    assert "dashboard_creator" not in client.get("/api/case_settings").json()


def test_saving_one_setting_leaves_the_other_alone(client):
    client.post("/api/case_settings", json={"ts_format": "date"})
    client.post("/api/case_settings", json={"dashboard_creator": True})
    assert client.get("/api/case_settings").json() == {"ts_format": "date", "dashboard_creator": "1"}
    # The reverse direction too: the ts_format write doesn't touch the flag.
    client.post("/api/case_settings", json={"ts_format": ""})
    assert client.get("/api/case_settings").json() == {"dashboard_creator": "1"}
    # And an empty body is a no-op, not a blanket clear.
    client.post("/api/case_settings", json={})
    assert client.get("/api/case_settings").json() == {"dashboard_creator": "1"}
