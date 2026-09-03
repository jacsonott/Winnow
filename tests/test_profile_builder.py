"""What the profile builder saves and reads back: a profile's description,
its named dashboards and its variable definitions, plus the library-board
route the builder embeds widgets from."""

from __future__ import annotations

from winnow.workspace import DashboardLibrary, PluginBundles

W = [{"title": "Rows", "source": "sql", "render": "stat", "query": {"sql": "SELECT 1"}}]


def test_a_profile_round_trips_everything_the_builder_sets():
    pb = PluginBundles()
    rec = pb.save("Ransomware triage", ["lateral_movement"],
                  description="What we open a ransomware case with",
                  dashboards=[{"name": "Host overview", "widgets": W}],
                  variables=[{"name": "engagement", "label": "Engagement", "required": True,
                              "description": "for report titles", "default": ""}])
    assert rec["description"] == "What we open a ransomware case with"
    assert [d["name"] for d in rec["dashboards"]] == ["Host overview"]
    assert rec["variables"][0]["required"] is True
    again = PluginBundles().get(rec["id"])
    assert again["description"] == rec["description"] and again["dashboards"] == rec["dashboards"]


def test_editing_keeps_fields_the_builder_did_not_send():
    pb = PluginBundles()
    rec = pb.save("Edit me", ["a"], description="first", dashboards=[{"name": "B", "widgets": W}],
                  variables=[{"name": "x"}])
    # A save that omits a field leaves it alone; one that sends it replaces it.
    same = pb.save("Edit me", ["a", "b"])
    assert same["description"] == "first" and same["dashboards"] and same["variables"]
    assert same["plugins"] == ["a", "b"]
    cleared = pb.save("Edit me", ["a"], description="", dashboards=[], variables=[])
    assert cleared["description"] == "" and cleared["dashboards"] == [] and cleared["variables"] == []
    assert cleared["id"] == rec["id"]      # upsert by name, not a second profile


def test_a_description_is_capped():
    rec = PluginBundles().save("Long", [], description="x" * 900)
    assert len(rec["description"]) == 400


def test_bundles_written_before_these_fields_existed_still_list(tmp_path, monkeypatch):
    """A workspace file from an older build has no description/dashboards/
    variables keys; the list must not KeyError in the builder or the menu."""
    import json
    from winnow import workspace as WS
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "plugin_bundles.json").write_text(json.dumps(
        {"bundles": [{"id": 1, "name": "Legacy", "plugins": ["a"], "dashboard": []}]}))
    monkeypatch.setattr(WS, "WORKSPACE_DIR", tmp_path / "workspace")
    legacy = next(b for b in PluginBundles().list() if b["name"] == "Legacy")
    assert legacy["description"] == "" and legacy["dashboards"] == [] and legacy["variables"] == []


def test_library_board_route_returns_widgets(client):
    rec = DashboardLibrary().save("Saved board", W)
    got = client.get(f"/api/dashboard_library/{rec['id']}").json()
    assert got["name"] == "Saved board" and got["widgets"] == W
    # the list route stays counts-only, so the builder has to ask for widgets
    listed = client.get("/api/dashboard_library").json()
    assert "widgets" not in listed[0] and listed[0]["widget_count"] == 1
    assert client.get("/api/dashboard_library/9999").status_code == 404


def test_save_route_takes_a_description(client):
    r = client.post("/api/plugin_bundles", json={
        "name": "Via the route", "plugins": [], "description": "from the builder",
        "dashboards": [{"name": "B", "widgets": W}],
        "variables": [{"name": "engagement", "required": True}]})
    assert r.status_code == 200
    listed = next(b for b in client.get("/api/plugin_bundles").json() if b["id"] == r.json()["id"])
    assert listed["description"] == "from the builder"
    assert listed["dashboards"][0]["name"] == "B" and listed["variables"][0]["required"] is True
