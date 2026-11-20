"""A case group's folders: where + New case files the group's cases (the
first one), and the folders the folder browser pins for every case in the
group (all of them).

The first folder comes from the group's first case, taken when that case
is filed in the group and kept after, wherever later cases go. More are
added, removed and reordered from the group's list on the home screen
(POST /api/case_groups/folders). A group whose folders were all removed
takes the next case's folder; one from before the record reads as its
oldest case's.

The registry keeps the folders beside the case list in cases.json; the
other writes to that file (a case opened, renamed, deleted) must not drop
them.
"""
from __future__ import annotations

import json

from winnow import workspace as WS


def _create(client, path, name, group=""):
    r = client.post("/api/cases", json={"path": str(path), "name": name, "group": group})
    assert r.status_code == 200, r.text
    return r.json()


def _folders(client, group):
    return client.get("/api/case_groups").json()["groups"][group]["folders"]


def _paths(client, group):
    return [f["path"] for f in _folders(client, group)]


def _change(client, group, **op):
    r = client.post("/api/case_groups/folders", json={"group": group, **op})
    assert r.status_code == 200, r.text
    return [f["path"] for f in r.json()["groups"][group]["folders"]]


# ------------------------------------------------------------ the first case


def test_the_first_cases_folder_becomes_the_groups(client, tmp_path):
    first = _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    (got,) = _folders(client, "ACME")
    assert got["path"] == str(tmp_path / "acme")
    assert got["from_case"] == {"id": first["id"], "name": "WS01"}


def test_a_later_case_filed_elsewhere_does_not_move_the_groups_folder(client, tmp_path):
    _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    _create(client, tmp_path / "elsewhere" / "dc01.db-winnow", "DC01", "ACME")
    assert _paths(client, "ACME") == [str(tmp_path / "acme")]


def test_the_folder_stays_when_the_case_that_set_it_leaves_the_group(client, tmp_path):
    """Taken once, kept: the first case moving on doesn't hand the group
    the next case's folder."""
    first = _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    _create(client, tmp_path / "other" / "dc01.db-winnow", "DC01", "ACME")
    assert client.put(f"/api/cases/{first['id']}", json={"group": ""}).status_code == 200
    assert _paths(client, "ACME") == [str(tmp_path / "acme")]


def test_moving_a_case_into_a_new_group_gives_the_group_its_folder(client, tmp_path):
    rec = _create(client, tmp_path / "initech" / "ws03.db-winnow", "WS03")
    assert client.get("/api/case_groups").json()["groups"] == {}
    assert client.put(f"/api/cases/{rec['id']}", json={"group": "Initech"}).status_code == 200
    assert _paths(client, "Initech") == [str(tmp_path / "initech")]


def test_a_group_from_before_the_record_takes_its_oldest_cases_folder(client, tmp_path):
    """Groups made before group folders existed have no record. They read
    as the folder of the oldest case in them, so every group has one from
    the start."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    WS.cases.create(str(tmp_path / "a" / "one.db-winnow"), "One", "Old group")
    WS.cases.create(str(tmp_path / "b" / "two.db-winnow"), "Two", "Old group")
    (got,) = _folders(client, "Old group")
    assert got["path"] == str(tmp_path / "a")
    assert got["from_case"]["name"] == "One"


def test_a_group_with_no_cases_left_is_not_listed(client, tmp_path):
    rec = _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    assert client.delete(f"/api/cases/{rec['id']}").status_code == 200
    assert "ACME" not in client.get("/api/case_groups").json()["groups"]


def test_other_registry_writes_keep_the_group_folders(client, tmp_path):
    """cases.json holds the case list and the groups' folders; opening,
    editing and deleting cases rewrite the file and must carry them."""
    (tmp_path / "share").mkdir()
    rec = _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    _change(client, "ACME", add=str(tmp_path / "share"))
    other = _create(client, tmp_path / "x" / "x.db-winnow", "X")
    WS.cases.touch_opened(rec["id"])
    client.put(f"/api/cases/{rec['id']}", json={"notes": "patient zero"})
    client.delete(f"/api/cases/{other['id']}")
    raw = json.loads((WS.WORKSPACE_DIR / "cases.json").read_text())
    assert [f["path"] for f in raw["groups"]["ACME"]["folders"]] == [str(tmp_path / "acme"), str(tmp_path / "share")]


# ------------------------------------------------------------ more than one


def test_adding_folders_keeps_the_first_one_first(client, tmp_path):
    for name in ("share", "triage"):
        (tmp_path / name).mkdir()
    _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    _change(client, "ACME", add=str(tmp_path / "share"))
    assert _change(client, "ACME", add=str(tmp_path / "triage")) == [
        str(tmp_path / "acme"), str(tmp_path / "share"), str(tmp_path / "triage")]
    # Once each: adding one the group has changes nothing.
    assert _change(client, "ACME", add=str(tmp_path / "share") + "/") == _paths(client, "ACME")
    assert len(_paths(client, "ACME")) == 3
    added = _folders(client, "ACME")[1]
    assert added["from_case"] is None and added["added_at"]


def test_any_folder_can_be_made_the_first(client, tmp_path):
    (tmp_path / "share").mkdir()
    _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    _change(client, "ACME", add=str(tmp_path / "share"))
    assert _change(client, "ACME", make_first=str(tmp_path / "share")) == [
        str(tmp_path / "share"), str(tmp_path / "acme")]


def test_a_removed_folder_is_gone_and_the_next_one_is_first(client, tmp_path):
    (tmp_path / "share").mkdir()
    _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    _change(client, "ACME", add=str(tmp_path / "share"))
    assert _change(client, "ACME", remove=str(tmp_path / "acme")) == [str(tmp_path / "share")]
    # A later case doesn't bring the removed one back: the group has one.
    _create(client, tmp_path / "acme" / "dc01.db-winnow", "DC01", "ACME")
    assert _paths(client, "ACME") == [str(tmp_path / "share")]


def test_with_every_folder_removed_the_next_case_sets_one(client, tmp_path):
    _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    assert _change(client, "ACME", remove=str(tmp_path / "acme")) == []
    cur = client.get("/api/case_groups").json()["groups"]
    assert cur["ACME"] == {"folders": []}, "listed, with none, rather than reading as its oldest case again"
    dc = _create(client, tmp_path / "later" / "dc01.db-winnow", "DC01", "ACME")
    (got,) = _folders(client, "ACME")
    assert got["path"] == str(tmp_path / "later")
    assert got["from_case"]["id"] == dc["id"]


def test_removing_a_folder_from_a_group_before_the_record(client, tmp_path):
    """The oldest case's folder is written down first, so taking it off
    leaves the group with none, not with it again."""
    (tmp_path / "a").mkdir()
    WS.cases.create(str(tmp_path / "a" / "one.db-winnow"), "One", "Old group")
    assert _change(client, "Old group", remove=str(tmp_path / "a")) == []


def test_changes_are_refused_when_they_cannot_be_made(client, tmp_path):
    _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    post = lambda body: client.post("/api/case_groups/folders", json=body)  # noqa: E731
    r = post({"group": "ACME", "add": str(tmp_path / "nope")})
    assert r.status_code == 400 and "No folder" in r.json()["detail"]
    assert post({"group": "Globex", "add": str(tmp_path)}).status_code == 404
    assert post({"group": "ACME", "remove": str(tmp_path / "never")}).status_code == 404
    assert post({"group": "ACME", "make_first": str(tmp_path / "never")}).status_code == 404
    assert post({"group": "ACME"}).status_code == 400
    assert post({"group": "ACME", "add": str(tmp_path), "remove": str(tmp_path / "acme")}).status_code == 400
    assert post({"group": " ", "add": str(tmp_path)}).status_code == 400
    assert _paths(client, "ACME") == [str(tmp_path / "acme")]


# ------------------------------------------------------------ the open case


def test_the_open_case_says_its_groups_folders(client, store, tmp_path):
    """/api/case/current is what the folder browser asks: the open case's
    group and that group's folders, which it pins, first one first."""
    (tmp_path / "share").mkdir()
    WS.cases.create(store.path, "Open one", "ACME")
    _change(client, "ACME", add=str(tmp_path / "share"))
    cur = client.get("/api/case/current").json()
    assert cur["group"] == "ACME"
    assert cur["group_folders"] == [str(tmp_path), str(tmp_path / "share")]


def test_the_open_case_with_no_group_has_no_folders(client, store):
    WS.cases.create(store.path, "Loose")
    cur = client.get("/api/case/current").json()
    assert (cur["group"], cur["group_folders"]) == ("", [])
