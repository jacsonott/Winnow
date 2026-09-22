"""Profiles as a portable thing: the one-file export/import, the lineage a
copy of a shipped profile records, and the apply sheet's two halves — the
plan that says what applying would change, and an apply that can leave a
part of it out.

The pointy end of all of this is that applying a profile REWRITES THE OPEN
CASE: an explicit on/off override for every installed plugin, boards
replaced by name, indicators seeded and a scan started. These tests pin
that each of those is separately declinable and that the numbers shown
beforehand are the ones the apply then produces.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from winnow import workspace as WS
from winnow.plugin_api import PluginRegistry

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"


@pytest.fixture
def registry(monkeypatch):
    """The same pinning tests/test_plugin_bundles.py uses: apply() ends in
    _reload_plugins(), which rescans PLUGIN_DIRS, so the machine's real
    plugins would otherwise leak into the response."""
    import server

    reg = PluginRegistry()
    reg.load([EXAMPLES])
    monkeypatch.setattr(server, "PLUGINS", reg)
    monkeypatch.setattr(server, "PLUGIN_DIRS", [EXAMPLES])
    monkeypatch.setattr(server, "BUNDLED_PLUGIN_DIR", EXAMPLES)
    return reg


def _profile(client, **over):
    body = {"name": "Ransomware", "plugins": ["lateral_movement"],
            "description": "what I run on a ransomware case",
            "dashboard": [{"title": "Events", "source": "sql", "render": "stat",
                           "query": {"sql": "SELECT 1"}},
                          {"title": "IOC hits", "source": "watchlist", "render": "stat", "live": True}],
            "dashboards": [{"name": "Extra", "widgets": [{"title": "Two", "render": "kv"}]}],
            "watchlist": [{"value": "rclone", "kind": "other"}, {"value": "psexec", "kind": "other"}],
            "variables": [{"name": "engagement", "label": "Engagement", "required": True}]}
    body.update(over)
    r = client.post("/api/plugin_bundles", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ export


def test_a_profile_round_trips_through_its_file(client, tmp_path):
    """Export, import, and the second profile carries every part of the
    first — a board that arrives without its watchlist is the failure this
    is guarding, since that is what the builder used to do to a copy."""
    rec = _profile(client)
    r = client.get(f"/api/plugin_bundles/{rec['id']}/export")
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers["content-disposition"]
    assert "winnow-profile-Ransomware.json" in r.headers["content-disposition"]
    data = r.json()
    assert data["format"] == "winnow-profile/1"
    # The file describes a profile, not a record on this machine.
    assert "id" not in data["profile"] and "shipped" not in data["profile"]

    path = tmp_path / "p.json"
    path.write_bytes(r.content)
    with open(path, "rb") as f:
        back = client.post("/api/plugin_bundles/import", files={"file": ("p.json", f, "application/json")})
    assert back.status_code == 200, back.text
    got = back.json()
    assert got["id"] != rec["id"]
    # Not an upsert: the name was taken, so the import sits beside it
    # rather than overwriting an afternoon's work.
    assert got["name"] == "Ransomware (imported)"
    for key in ("plugins", "description", "dashboard", "dashboards", "watchlist", "variables"):
        assert got[key] == rec[key], key


def test_importing_a_key_this_version_does_not_understand_is_refused(client):
    """Refused, not dropped. A file from a newer Winnow that carries a part
    this one cannot apply would otherwise import as a profile that looks
    whole and silently is not."""
    r = client.post("/api/plugin_bundles/import", files={"file": (
        "p.json", b'{"format": "winnow-profile/1", "profile": {"name": "X", "filters": []}}',
        "application/json")})
    assert r.status_code == 400
    assert "filters" in r.json()["detail"]
    assert not any(b["name"] == "X" for b in client.get("/api/plugin_bundles").json())


def test_a_file_that_is_not_a_profile_is_refused_by_name(client):
    for body, expect in [
        (b'{"format": "winnow-filters/1", "filters": []}', "Not a Winnow profile file"),
        (b'{"format": "winnow-profile/1"}', "no profile in it"),
        (b'{"format": "winnow-profile/1", "profile": {"name": "  "}}', "no name"),
        (b"not json at all", "Not a valid profile file"),
        # A KNOWN key holding the wrong kind of value. `save` is
        # permissive on purpose, so `"plugins": "lateral_movement"` would
        # store `sorted({str(p) for p in plugins})` — one plugin per
        # LETTER — and a string `dashboard` would reach the manager as a
        # board whose widgets cannot be iterated. Half a profile that
        # looks whole, which is the failure the key check is named for.
        (b'{"format": "winnow-profile/1", "profile": {"name": "X", "plugins": "lateral_movement"}}',
         "“plugins” in that profile file is a string, not a list"),
        (b'{"format": "winnow-profile/1", "profile": {"name": "X", "dashboard": {"a": 1}}}',
         "“dashboard” in that profile file is an object, not a list"),
        (b'{"format": "winnow-profile/1", "profile": {"name": "X", "watchlist": ["rclone"]}}',
         "“watchlist” in that profile file holds a string"),
        (b'{"format": "winnow-profile/1", "profile": {"name": "X", "from_version": true}}',
         "“from_version” in that profile file is a true/false value, not a number"),
    ]:
        r = client.post("/api/plugin_bundles/import",
                        files={"file": ("p.json", body, "application/json")})
        assert r.status_code == 400, body
        assert expect in r.json()["detail"], body
        assert not any(b["name"] == "X" for b in client.get("/api/plugin_bundles").json()), body


def test_a_name_that_fills_the_cap_still_finds_a_free_one(client):
    """A profile name may be exactly 100 characters, and the suffix the
    import adds to a taken name was appended and then truncated back to
    100 — straight back to the name it collided with, so every candidate
    was taken and the search never ended. The route is a plain `def`, so
    that pinned a threadpool worker at 100% for the life of the process
    and a second attempt took another one.

    Asserted with a watchdog because the symptom is "never returns"."""
    long_name = "A" * 100
    assert client.post("/api/plugin_bundles",
                       json={"name": long_name, "plugins": []}).status_code == 200
    file = json.dumps({"format": "winnow-profile/1",
                       "profile": {"name": long_name}}).encode()

    out = {}

    def go():
        r = client.post("/api/plugin_bundles/import",
                        files={"file": ("p.json", file, "application/json")})
        out["status"], out["body"] = r.status_code, r.json()

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(30)
    assert not t.is_alive(), "the import never returned — the search for a free name cannot end"
    assert out["status"] == 200, out
    assert out["body"]["name"] != long_name and len(out["body"]["name"]) <= 100
    # and a second one lands beside both rather than on either
    names = {b["name"] for b in client.get("/api/plugin_bundles").json()}
    r = client.post("/api/plugin_bundles/import", files={"file": ("p.json", file, "application/json")})
    assert r.status_code == 200 and r.json()["name"] not in names


def test_a_name_with_room_to_spare_is_imported_unchanged(client):
    """The trimming is for the collision only — an ordinary import keeps
    the name it arrived with, character for character."""
    r = client.post("/api/plugin_bundles/import", files={"file": (
        "p.json", json.dumps({"format": "winnow-profile/1",
                              "profile": {"name": "B" * 100}}).encode(), "application/json")})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "B" * 100


def test_a_shipped_profile_exports_as_an_editable_copy(client):
    """A shipped profile is read-only here and must be on the next machine
    too — so its file is a COPY that remembers its ancestry, which is the
    same thing "Copy to edit" produces."""
    kape = next(b for b in client.get("/api/plugin_bundles").json() if b["name"] == "KAPE triage")
    assert kape["id"] < 0 and kape["shipped"] and kape["version"] >= 1
    prof = client.get(f"/api/plugin_bundles/{kape['id']}/export").json()["profile"]
    assert prof["from_profile"] == "KAPE triage"
    assert prof["from_version"] == kape["version"]
    assert len(prof["dashboard"]) == len(kape["dashboard"])
    assert [i["value"] for i in prof["watchlist"]] == [i["value"] for i in kape["watchlist"]]

    r = client.post("/api/plugin_bundles/import", files={"file": (
        "p.json", client.get(f"/api/plugin_bundles/{kape['id']}/export").content, "application/json")})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["id"] > 0 and not got.get("shipped")
    # The imported copy is editable in place, unlike the one it came from.
    assert client.post("/api/plugin_bundles", json={"name": got["name"], "plugins": []}).json()["id"] == got["id"]


# ----------------------------------------------------------------- lineage


def test_a_copy_records_where_it_came_from_and_is_offered_the_update(client):
    """The version is recorded on the copy, not derived from its contents:
    a copy is meant to be edited, and a content comparison would lose the
    thread on the first edit."""
    kape = next(b for b in client.get("/api/plugin_bundles").json() if b["name"] == "KAPE triage")
    mine = _profile(client, name="KAPE triage (my copy)", plugins=kape["plugins"],
                    dashboard=kape["dashboard"], watchlist=kape["watchlist"],
                    variables=kape["variables"], dashboards=[],
                    from_profile="KAPE triage", from_version=kape["version"])
    listed = next(b for b in client.get("/api/plugin_bundles").json() if b["id"] == mine["id"])
    assert listed["from_profile"] == "KAPE triage"
    assert listed["update_available"] is None, "same version — nothing to offer"

    # The shipped profile moves on (what PR 4's board change will do).
    stale = _profile(client, name="KAPE triage (older copy)", plugins=[], dashboard=[],
                     dashboards=[], watchlist=[], variables=[],
                     from_profile="KAPE triage", from_version=kape["version"] - 1)
    listed = next(b for b in client.get("/api/plugin_bundles").json() if b["id"] == stale["id"])
    assert listed["update_available"] == {"name": "KAPE triage",
                                          "taken_at": kape["version"] - 1,
                                          "now": kape["version"]}

    d = client.get(f"/api/plugin_bundles/{stale['id']}/diff").json()
    assert d["from"] == "KAPE triage" and d["now"] == kape["version"]
    assert d["plugins"]["added"] == kape["plugins"]
    assert d["board"] == {"was": 0, "now": len(kape["dashboard"]),
                          "added": [w["title"] for w in kape["dashboard"]], "removed": []}
    assert d["watchlist"]["added"] == [i["value"] for i in kape["watchlist"]]

    # Taking it is a button — and it replaces, keeping the id and the name.
    took = client.post(f"/api/plugin_bundles/{stale['id']}/take_update")
    assert took.status_code == 200, took.text
    assert took.json()["id"] == stale["id"] and took.json()["name"] == "KAPE triage (older copy)"
    after = next(b for b in client.get("/api/plugin_bundles").json() if b["id"] == stale["id"])
    assert len(after["dashboard"]) == len(kape["dashboard"])
    assert after["from_version"] == kape["version"] and after["update_available"] is None


def test_a_profile_with_no_lineage_has_no_diff_to_show(client):
    rec = _profile(client)
    assert client.get(f"/api/plugin_bundles/{rec['id']}/diff").status_code == 404
    assert client.post(f"/api/plugin_bundles/{rec['id']}/take_update").status_code == 400
    kape = next(b for b in client.get("/api/plugin_bundles").json() if b["name"] == "KAPE triage")
    assert client.get(f"/api/plugin_bundles/{kape['id']}/diff").status_code == 404, \
        "a shipped profile has nothing upstream of it"


def test_a_copy_of_a_profile_this_winnow_no_longer_ships_is_just_a_profile(client, monkeypatch):
    """The lineage names a profile, not an id, so an install that drops one
    leaves the copy working — with no update line and no diff, rather than
    a 500 or a dangling reference."""
    rec = _profile(client, from_profile="Gone", from_version=1)
    listed = next(b for b in client.get("/api/plugin_bundles").json() if b["id"] == rec["id"])
    assert listed["update_available"] is None
    assert client.get(f"/api/plugin_bundles/{rec['id']}/diff").status_code == 404


# -------------------------------------------------------------- apply plan


def test_the_plan_counts_what_apply_will_change(client, store, write_csv, registry):
    store.ingest_csv(write_csv([["a"], ["1"]], "e.csv"), name="e", build_fts=False)
    store.add_indicator("psexec", "other", None, None)
    rec = _profile(client)

    installed = {p["fs_name"] for p in registry.describe()}
    on_now = {p["fs_name"] for p in registry.describe() if p["enabled"]}
    plan = client.get(f"/api/plugin_bundles/{rec['id']}/plan").json()
    assert plan["name"] == "Ransomware"
    # Both directions, against what this case actually has on right now —
    # "everything not in the profile is turned off" is the part of apply
    # that was never stated before the sheet existed.
    assert sorted(plan["plugins"]["turn_on"] + plan["plugins"]["already_on"]) == ["lateral_movement"]
    assert set(plan["plugins"]["turn_off"]) == on_now - {"lateral_movement"}
    assert plan["plugins"]["stay_off"] == len(installed - on_now - {"lateral_movement"})
    assert plan["boards"] == [
        {"name": "Ransomware", "widgets": 2, "live": 1, "replaces": None},
        {"name": "Extra", "widgets": 1, "live": 0, "replaces": None},
    ]
    assert plan["watchlist"]["new"] == ["rclone"]
    assert plan["watchlist"]["existing"] == ["psexec"]
    assert plan["watchlist"]["scan_tables"] == 1
    assert plan["variables"] == [{"name": "engagement", "label": "Engagement",
                                 "required": True, "set": False, "default": ""}]
    # Every installed plugin gets an explicit override, not just the ones
    # that move — the number the sheet keeps its Plugins part tickable on.
    assert plan["plugins"]["pins"] == len(installed)

    client.post(f"/api/plugin_bundles/{rec['id']}/apply")

    # Applied, the plan describes the SAME case differently: nothing new to
    # add, and the boards it would replace are its own untouched copies.
    plan = client.get(f"/api/plugin_bundles/{rec['id']}/plan").json()
    assert plan["plugins"]["turn_on"] == [] and plan["plugins"]["already_on"] == ["lateral_movement"]
    assert plan["plugins"]["turn_off"] == [], "the rest are off now, so there is nothing left to turn off"
    assert plan["watchlist"]["new"] == [] and plan["watchlist"]["scan_tables"] == 0
    assert plan["boards"][0]["replaces"] == {"widget_count": 2, "edited": False}
    assert plan["variables"][0]["set"] is False, "seeding a definition does not invent a value"

    # Edit one card and the same board is now the analyst's, by name.
    board = next(d for d in store.list_dashboards() if d["name"] == "Ransomware")
    store.set_dashboard_widgets(board["id"], [{"title": "Mine", "render": "stat"}])
    plan = client.get(f"/api/plugin_bundles/{rec['id']}/plan").json()
    assert plan["boards"][0]["replaces"] == {"widget_count": 1, "edited": True}


def test_the_plans_scan_count_is_the_scan_the_apply_runs(client, store, write_csv, registry):
    """Merge parity, invariant #9: a merge has no src_N of its own and its
    rows are scanned through its members, so the sheet must not promise a
    table that is really two of the ones already counted."""
    a = store.ingest_csv(write_csv([["a"], ["1"]], "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["a"], ["2"]], "b.csv"), name="b", build_fts=False)["id"]
    merged = store.create_merge("m", [a, b])
    assert merged["id"] < 0
    assert [s["id"] for s in store.watchlist_scan_sources()] == [a, b]

    rec = _profile(client, watchlist=[{"value": "1", "kind": "other"}])
    plan = client.get(f"/api/plugin_bundles/{rec['id']}/plan").json()
    assert plan["watchlist"]["scan_tables"] == 2, "the merge is its members, counted once"


def test_the_plan_needs_a_case(client, monkeypatch):
    import server

    rec = _profile(client)
    monkeypatch.setattr(server, "STORE", None)
    assert client.get(f"/api/plugin_bundles/{rec['id']}/plan").status_code == 400


def test_a_required_variable_with_a_default_is_not_promised_a_prompt(client, store, registry):
    """The sheet prints "you will be asked for these after applying" from
    the plan's `set`. Apply seeds the row WITH the declared default, so a
    required variable that has one is never asked for — the sheet has to
    say which of the two happens."""
    rec = _profile(client, variables=[{"name": "engagement", "label": "Engagement",
                                       "required": True, "default": "Acme IR"}])
    plan = client.get(f"/api/plugin_bundles/{rec['id']}/plan").json()
    assert plan["variables"] == [{"name": "engagement", "label": "Engagement",
                                  "required": True, "set": False, "default": "Acme IR"}]

    applied = client.post(f"/api/plugin_bundles/{rec['id']}/apply").json()
    assert applied["variables_missing"] == [], "nothing prompts, so the sheet must not promise one"
    assert [v["value"] for v in store.list_variables() if v["name"] == "engagement"] == ["Acme IR"]


def test_a_variable_the_apply_would_skip_is_not_in_the_plan(client, store, registry):
    """Store.seed_variables skips a name outside VARIABLE_NAME_RE, and a
    profile that arrived as a file has had its names checked nowhere (the
    builder's guard is client-side). Listing one would promise a variable
    that is never created and never asked for."""
    rec = _profile(client, variables=[{"name": "2 bad", "required": True},
                                      {"name": "good_one", "required": True}])
    plan = client.get(f"/api/plugin_bundles/{rec['id']}/plan").json()
    assert [v["name"] for v in plan["variables"]] == ["good_one"]

    client.post(f"/api/plugin_bundles/{rec['id']}/apply")
    assert [v["name"] for v in store.list_variables()] == ["good_one"]


# ------------------------------------------------------------- apply parts


def test_applying_one_part_leaves_the_others_exactly_alone(client, store, write_csv, registry):
    """The sheet's whole point: take the board, leave my plugins and my
    watchlist where they are."""
    store.ingest_csv(write_csv([["a"], ["1"]], "e.csv"), name="e", build_fts=False)
    rec = _profile(client)

    r = client.post(f"/api/plugin_bundles/{rec['id']}/apply", json={"parts": ["boards"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["parts"] == ["boards"]
    assert out["dashboard_applied"] is True and out["dashboards_applied"] == ["Extra"]
    assert out["watchlist_seeded"] == 0 and out["variables_missing"] == []
    assert sorted(d["name"] for d in store.list_dashboards()) == ["Extra", "Ransomware"]
    assert store.list_indicators() == []
    assert store.list_variables() == []
    # No override was written for any plugin: the case's plugin set is
    # still whatever the machine says, which is what "leave it alone" means.
    assert store.get_case_settings().get("plugin_overrides") is None

    # ...and the parts left out are still available afterwards.
    r = client.post(f"/api/plugin_bundles/{rec['id']}/apply", json={"parts": ["watchlist", "variables"]})
    assert r.json()["watchlist_seeded"] == 2
    assert r.json()["variables_missing"] == ["engagement"]
    assert store.get_case_settings().get("plugin_overrides") is None


def test_an_apply_with_no_body_is_still_the_whole_profile(client, store, write_csv, registry):
    """The new-case dialog's Case type select and every script predate the
    sheet and send nothing; they must keep meaning "all of it"."""
    store.ingest_csv(write_csv([["a"], ["1"]], "e.csv"), name="e", build_fts=False)
    rec = _profile(client)
    out = client.post(f"/api/plugin_bundles/{rec['id']}/apply").json()
    assert out["parts"] == ["plugins", "boards", "watchlist", "variables"]
    assert out["dashboard_applied"] and out["watchlist_seeded"] == 2
    assert out["variables_missing"] == ["engagement"]
    overrides = store.get_case_settings()["plugin_overrides"]
    assert "lateral_movement" in overrides
    assert store.get_case_settings()["profiles_applied"].count("Ransomware") == 1


def test_a_part_nobody_ships_is_a_400_not_a_silent_no_op(client, store, registry):
    rec = _profile(client)
    r = client.post(f"/api/plugin_bundles/{rec['id']}/apply", json={"parts": ["plugins", "filters"]})
    assert r.status_code == 400 and "filters" in r.json()["detail"]
    assert store.get_case_settings().get("plugin_overrides") is None


def test_an_applied_board_is_the_profiles_until_it_is_edited(client, store, write_csv, registry):
    """`origin` is what makes "you have edited this" answerable at all, and
    a plugin offering a board of the same name must still ask — a profile
    stamp is not a plugin stamp."""
    store.ingest_csv(write_csv([["a"], ["1"]], "e.csv"), name="e", build_fts=False)
    rec = _profile(client)
    client.post(f"/api/plugin_bundles/{rec['id']}/apply", json={"parts": ["boards"]})
    assert store.find_dashboard_by_name("Ransomware")["origin"] == "profile:Ransomware"
    board = store.find_dashboard_by_name("Ransomware")
    store.set_dashboard_widgets(board["id"], [])
    assert store.find_dashboard_by_name("Ransomware")["origin"] is None


def test_the_watchlist_part_alone_still_scans(client, store, write_csv, registry):
    store.ingest_csv(write_csv([["a"], ["rclone.exe ran"]], "e.csv"), name="e", build_fts=False)
    rec = _profile(client)
    client.post(f"/api/plugin_bundles/{rec['id']}/apply", json={"parts": ["watchlist"]})
    hits = {i["value"]: i["hit_count"] for i in store.list_indicators()}
    assert hits["rclone"] == 1, hits


def test_a_profile_saved_without_a_watchlist_keeps_the_one_it_had(client):
    """Every field is None-means-leave-alone, so the builder — which has no
    watchlist editor — cannot drop a profile's indicators by saving a
    description."""
    rec = _profile(client)
    again = client.post("/api/plugin_bundles", json={"name": "Ransomware", "plugins": ["pivot"],
                                                    "description": "reworded"}).json()
    assert again["id"] == rec["id"]
    assert [i["value"] for i in again["watchlist"]] == ["rclone", "psexec"]
    assert len(again["dashboard"]) == 2


def test_older_records_gain_the_new_fields_on_read(client):
    """workspace/plugin_bundles.json is a file on an analyst's disk that
    predates every field added since — list() fills them in rather than
    letting the client read undefined."""
    WS.plugin_bundles  # the store the route uses
    path = WS._ensure_dir() / "plugin_bundles.json"
    path.write_text('{"bundles": [{"id": 7, "name": "Old", "plugins": ["pivot"]}]}', encoding="utf-8")
    old = next(b for b in client.get("/api/plugin_bundles").json() if b["id"] == 7)
    assert old["watchlist"] == [] and old["dashboards"] == [] and old["variables"] == []
    assert old["from_profile"] is None and old["update_available"] is None
