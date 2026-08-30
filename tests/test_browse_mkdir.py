"""Creating a folder from the folder picker.

The endpoint writes to disk, which the rest of /api/browse_dir does not,
so the guards are the point: loopback only, one path segment, and a
result that provably lands inside the folder being browsed."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

HEADERS = {"X-Timeline-Lite-Client": "1"}


@pytest.fixture
def client(store, monkeypatch):
    import server
    monkeypatch.setattr(server, "STORE", store)
    return server, TestClient(server.app)


def test_it_creates_the_folder(client, tmp_path):
    server, c = client
    res = c.post("/api/browse_dir/new",
                 json={"parent": str(tmp_path), "name": "Intrusion 2026"}, headers=HEADERS)
    assert res.status_code == 200
    made = tmp_path / "Intrusion 2026"
    assert made.is_dir()
    assert res.json() == {"path": str(made), "name": "Intrusion 2026"}


def test_it_refuses_a_name_that_would_escape_the_folder(client, tmp_path):
    """The name is one segment. Anything that would land the folder
    somewhere other than directly inside `parent` is refused — checked by
    where the result RESOLVES, not only by what the string looks like."""
    server, c = client
    # A parent of its own: tmp_path already holds the store fixture's case
    # file, so "nothing was created" has to be asked of an empty folder.
    parent = tmp_path / "browse-root"
    parent.mkdir()
    for name in ("../escaped", "sub/deeper", "..", "."):
        res = c.post("/api/browse_dir/new",
                     json={"parent": str(parent), "name": name}, headers=HEADERS)
        assert res.status_code == 400, name
    assert not (tmp_path / "escaped").exists()
    assert list(parent.iterdir()) == [], "nothing was created anywhere"


def test_it_refuses_an_existing_name(client, tmp_path):
    server, c = client
    (tmp_path / "taken").mkdir()
    res = c.post("/api/browse_dir/new",
                 json={"parent": str(tmp_path), "name": "taken"}, headers=HEADERS)
    assert res.status_code == 400 and "already exists" in res.json()["detail"]


def test_it_refuses_an_empty_name(client, tmp_path):
    server, c = client
    for name in ("", "   ", "..."):
        res = c.post("/api/browse_dir/new",
                     json={"parent": str(tmp_path), "name": name}, headers=HEADERS)
        assert res.status_code == 400, name


def test_it_refuses_a_parent_that_is_not_a_folder(client, tmp_path):
    server, c = client
    f = tmp_path / "a-file.txt"
    f.write_text("x")
    for parent in (str(f), str(tmp_path / "nope")):
        res = c.post("/api/browse_dir/new",
                     json={"parent": parent, "name": "x"}, headers=HEADERS)
        assert res.status_code == 400, parent


def test_it_is_loopback_only(client, tmp_path, monkeypatch):
    """Listing a folder answers any peer the analyst chose to bind; WRITING
    one is a different risk and stays local."""
    server, c = client
    monkeypatch.setattr(server, "_is_loopback", lambda request: False)
    res = c.post("/api/browse_dir/new",
                 json={"parent": str(tmp_path), "name": "remote"}, headers=HEADERS)
    assert res.status_code == 403
    assert not (tmp_path / "remote").exists()


def test_the_new_folder_is_immediately_browsable(client, tmp_path):
    """The picker steps into it after creating it, so it has to list."""
    server, c = client
    made = c.post("/api/browse_dir/new",
                  json={"parent": str(tmp_path), "name": "cases"}, headers=HEADERS).json()
    res = c.get(f"/api/browse_dir?path={made['path']}", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["path"] == made["path"]
