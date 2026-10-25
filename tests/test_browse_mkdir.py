"""Creating a folder from the folder picker.

The endpoint writes to disk, which the rest of /api/browse_dir does not,
so the guards are the point: loopback only, one path segment, and a
result that provably lands inside the folder being browsed."""

from __future__ import annotations


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


# The two routes that create folders as a SIDE effect. browse_dir/new
# refusing a LAN peer meant nothing while these two would makedirs the same
# folders for it. A real non-loopback peer address rather than a patched
# _is_loopback, so the check is exercised where the request carries it.

@pytest.fixture
def lan(store, monkeypatch):
    import server
    monkeypatch.setattr(server, "STORE", store)
    return TestClient(server.app, headers=HEADERS, client=("192.168.1.50", 40000))


def test_a_lan_peer_cannot_make_folders_by_creating_a_case(lan, tmp_path):
    target = tmp_path / "lan_made" / "deep"
    res = lan.post("/api/cases", json={"path": str(target / "x.db-winnow"), "name": "x"})
    assert res.status_code == 403
    assert not (tmp_path / "lan_made").exists()


def test_a_lan_peer_can_still_file_a_case_into_an_existing_folder(lan, tmp_path):
    """Only the folder is the new reach; a case in a folder that is already
    there is what a shared server is for."""
    res = lan.post("/api/cases", json={"path": str(tmp_path / "x.db-winnow"), "name": "x"})
    assert res.status_code == 200, res.text
    assert (tmp_path / "x.db-winnow").is_file()


def test_a_lan_peer_cannot_make_folders_through_the_cases_dir_pref(lan, tmp_path):
    target = tmp_path / "lan_prefs" / "a"
    res = lan.post("/api/prefs", json={"cases_dir": str(target)})
    assert res.status_code == 403
    assert not (tmp_path / "lan_prefs").exists()


def test_this_machine_still_makes_both(client, tmp_path):
    server, c = client
    res = c.post("/api/cases", headers=HEADERS,
                 json={"path": str(tmp_path / "new" / "x.db-winnow"), "name": "x"})
    assert res.status_code == 200, res.text
    res = c.post("/api/prefs", headers=HEADERS, json={"cases_dir": str(tmp_path / "cases" / "here")})
    assert res.status_code == 200, res.text
    assert (tmp_path / "new" / "x.db-winnow").is_file() and (tmp_path / "cases" / "here").is_dir()
