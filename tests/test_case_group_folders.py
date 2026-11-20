"""A case group's folder: where + New case files the group's cases, and the
folder the folder browser pins for every case in the group.

Two ways for a group to get one are on trial in a demo (Settings → Case
groups, the `group_folder_mode` preference), and each is pinned here as
it would ship on its own:

- first_case: the folder the group's first case is saved in, taken when
  that case is filed in the group and kept after, wherever later cases go.
- settings: only what was chosen in the group's settings.

The registry keeps both records beside the case list in cases.json; the
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


def _folders(client):
    return client.get("/api/case_groups").json()["folders"]


def _mode(client, mode):
    r = client.post("/api/prefs", json={"group_folder_mode": mode})
    assert r.status_code == 200, r.text
    assert r.json()["group_folder_mode"] == mode


# ------------------------------------------------------------ first_case


def test_first_case_is_the_default_and_its_folder_becomes_the_groups(client, tmp_path):
    assert client.get("/api/case_groups").json()["mode"] == "first_case"
    first = _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    got = _folders(client)["ACME"]
    assert got["folder"] == str(tmp_path / "acme")
    assert got["set_by"] == {"id": first["id"], "name": "WS01"}


def test_a_later_case_filed_elsewhere_does_not_move_the_groups_folder(client, tmp_path):
    _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    _create(client, tmp_path / "elsewhere" / "dc01.db-winnow", "DC01", "ACME")
    assert _folders(client)["ACME"]["folder"] == str(tmp_path / "acme")


def test_the_folder_stays_when_the_case_that_set_it_leaves_the_group(client, tmp_path):
    """Taken once, kept: the first case moving on doesn't hand the group
    the next case's folder."""
    first = _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    _create(client, tmp_path / "other" / "dc01.db-winnow", "DC01", "ACME")
    assert client.put(f"/api/cases/{first['id']}", json={"group": ""}).status_code == 200
    assert _folders(client)["ACME"]["folder"] == str(tmp_path / "acme")


def test_moving_a_case_into_a_new_group_gives_the_group_its_folder(client, tmp_path):
    rec = _create(client, tmp_path / "initech" / "ws03.db-winnow", "WS03")
    assert _folders(client) == {}
    assert client.put(f"/api/cases/{rec['id']}", json={"group": "Initech"}).status_code == 200
    assert _folders(client)["Initech"]["folder"] == str(tmp_path / "initech")


def test_a_group_from_before_the_record_takes_its_oldest_cases_folder(client, tmp_path):
    """Groups made before group folders existed have no record. They take
    the folder of the oldest case in them, so every group has one under
    first_case from the start."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    WS.cases.create(str(tmp_path / "a" / "one.db-winnow"), "One", "Old group")
    WS.cases.create(str(tmp_path / "b" / "two.db-winnow"), "Two", "Old group")
    got = _folders(client)["Old group"]
    assert got["folder"] == str(tmp_path / "a")
    assert got["set_by"]["name"] == "One"


def test_a_group_with_no_cases_left_has_no_folder_listed(client, tmp_path):
    rec = _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    assert client.delete(f"/api/cases/{rec['id']}").status_code == 200
    assert "ACME" not in _folders(client)


def test_other_registry_writes_keep_the_group_folders(client, tmp_path):
    """cases.json holds the case list and the groups' folders; opening,
    editing and deleting cases rewrite the file and must carry them."""
    rec = _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    other = _create(client, tmp_path / "x" / "x.db-winnow", "X")
    WS.cases.touch_opened(rec["id"])
    client.put(f"/api/cases/{rec['id']}", json={"notes": "patient zero"})
    client.delete(f"/api/cases/{other['id']}")
    raw = json.loads((WS.WORKSPACE_DIR / "cases.json").read_text())
    assert raw["group_folders"]["first_case"]["ACME"]["folder"] == str(tmp_path / "acme")


def test_the_open_case_says_its_groups_folder(client, store, tmp_path):
    """/api/case/current is what the folder browser asks: the open case's
    group and that group's folder, which it pins."""
    WS.cases.create(store.path, "Open one", "ACME")
    cur = client.get("/api/case/current").json()
    assert cur["group"] == "ACME"
    assert cur["group_folder"] == str(tmp_path)


def test_the_open_case_with_no_group_has_no_folder(client, store):
    WS.cases.create(store.path, "Loose")
    cur = client.get("/api/case/current").json()
    assert (cur["group"], cur["group_folder"]) == ("", None)


def test_first_case_does_not_take_a_folder_from_the_settings(client, tmp_path):
    r = client.post("/api/case_groups/folder", json={"group": "ACME", "folder": str(tmp_path)})
    assert r.status_code == 400
    assert "first case" in r.json()["detail"]


# ------------------------------------------------------------ settings


def test_settings_a_group_has_no_folder_until_one_is_chosen(client, tmp_path):
    _mode(client, "settings")
    _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    assert _folders(client) == {}
    cur = client.post("/api/case_groups/folder", json={"group": "ACME", "folder": str(tmp_path / "acme")})
    assert cur.status_code == 200, cur.text
    got = cur.json()["folders"]["ACME"]
    assert got["folder"] == str(tmp_path / "acme")
    assert got["set_by"] is None


def test_settings_clearing_the_folder(client, tmp_path):
    _mode(client, "settings")
    _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    client.post("/api/case_groups/folder", json={"group": "ACME", "folder": str(tmp_path)})
    r = client.post("/api/case_groups/folder", json={"group": "ACME", "folder": None})
    assert r.status_code == 200
    assert r.json()["folders"] == {}


def test_settings_refuses_a_folder_that_is_not_there_and_a_group_that_is_not(client, tmp_path):
    _mode(client, "settings")
    _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    r = client.post("/api/case_groups/folder", json={"group": "ACME", "folder": str(tmp_path / "nope")})
    assert r.status_code == 400 and "No folder" in r.json()["detail"]
    r = client.post("/api/case_groups/folder", json={"group": "Globex", "folder": str(tmp_path)})
    assert r.status_code == 404
    assert _folders(client) == {}


def test_settings_a_new_case_takes_nothing_for_its_group(client, tmp_path):
    _mode(client, "settings")
    _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    raw = json.loads((WS.WORKSPACE_DIR / "cases.json").read_text())
    assert "first_case" not in (raw.get("group_folders") or {})


def test_each_way_keeps_its_own_record(client, tmp_path):
    """Switching the demo between the two shows each as it would be on its
    own: a folder chosen in the settings doesn't appear under first_case,
    and the first case's doesn't appear under settings."""
    (tmp_path / "chosen").mkdir()
    _create(client, tmp_path / "acme" / "ws01.db-winnow", "WS01", "ACME")
    _mode(client, "settings")
    assert _folders(client) == {}
    client.post("/api/case_groups/folder", json={"group": "ACME", "folder": str(tmp_path / "chosen")})
    assert _folders(client)["ACME"]["folder"] == str(tmp_path / "chosen")
    _mode(client, "first_case")
    assert _folders(client)["ACME"]["folder"] == str(tmp_path / "acme")


def test_an_unknown_mode_is_refused(client):
    r = client.post("/api/prefs", json={"group_folder_mode": "whatever"})
    assert r.status_code == 400
    assert client.get("/api/case_groups").json()["mode"] == "first_case"
