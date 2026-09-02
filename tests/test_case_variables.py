"""Case variables: the store table and its rules, the HTTP routes, profile
seeding on apply, and what a plugin sees through PluginRequest."""

from __future__ import annotations

import pytest

from winnow import plugin_api
from winnow.workspace import PluginBundles


# ------------------------------------------------------------------ store

def test_set_list_get_and_delete(store):
    assert store.list_variables() == []
    rec = store.set_variable("engagement", "ACME-2026")
    assert rec == {"name": "engagement", "value": "ACME-2026", "description": "", "required": False}
    store.set_variable("report_api", "http://r.local", description="where reports go", required=True)
    assert store.get_variables() == {"engagement": "ACME-2026", "report_api": "http://r.local"}
    # required ones sort first, then by name
    assert [v["name"] for v in store.list_variables()] == ["report_api", "engagement"]
    store.delete_variable("engagement")
    assert list(store.get_variables()) == ["report_api"]
    store.delete_variable("never-existed")  # idempotent


def test_partial_updates_keep_what_was_not_given(store):
    store.set_variable("x", "1", description="first", required=True)
    store.set_variable("x", "2")
    assert store.list_variables()[0] == {"name": "x", "value": "2", "description": "first", "required": True}
    store.set_variable("x", description="second")
    assert store.list_variables()[0]["value"] == "2"
    assert store.list_variables()[0]["description"] == "second"
    store.set_variable("x", required=False)
    assert store.list_variables()[0]["required"] is False


@pytest.mark.parametrize("bad", ["", "1abc", "has space", "a/b", "x" * 65, "-lead"])
def test_names_are_validated(store, bad):
    with pytest.raises(ValueError):
        store.set_variable(bad, "v")


def test_values_are_capped(store):
    store.set_variable("big", "x" * 4000)
    with pytest.raises(ValueError):
        store.set_variable("big", "x" * 4001)
    assert len(store.get_variables()["big"]) == 4000


def test_seed_creates_without_overwriting_and_reports_required_gaps(store):
    store.set_variable("engagement", "already set")
    missing = store.seed_variables([
        {"name": "engagement", "required": True, "description": "from profile"},
        {"name": "report_api", "required": True},
        {"name": "doc_link", "default": "https://docs.local/x"},
    ])
    assert missing == ["report_api"]
    vs = {v["name"]: v for v in store.list_variables()}
    assert vs["engagement"]["value"] == "already set"        # never overwritten
    assert vs["engagement"]["description"] == "from profile"  # but definition refreshed
    assert vs["engagement"]["required"] is True
    assert vs["report_api"]["value"] == ""
    assert vs["doc_link"]["value"] == "https://docs.local/x"
    # seeding again is a no-op for values, and the gap is still reported
    assert store.seed_variables([{"name": "report_api", "required": True}]) == ["report_api"]
    store.set_variable("report_api", "http://r")
    assert store.seed_variables([{"name": "report_api", "required": True}]) == []


def test_seed_skips_malformed_definitions(store):
    assert store.seed_variables([{"name": "bad name", "required": True}, {}, "junk"]) == []
    assert store.list_variables() == []


def test_variables_survive_reopen(case_path):
    from winnow.store import Store
    s = Store(case_path)
    s.set_variable("engagement", "ACME")
    s.close()
    s2 = Store(case_path)
    try:
        assert s2.get_variables() == {"engagement": "ACME"}
    finally:
        s2.close()


# ------------------------------------------------------------------ routes

def test_routes_crud(client):
    assert client.get("/api/case/variables").json() == []
    r = client.post("/api/case/variables", json={"name": "engagement", "value": "ACME"})
    assert r.status_code == 200 and r.json()["value"] == "ACME"
    r = client.post("/api/case/variables", json={"name": "report_api", "required": True, "description": "d"})
    assert r.json() == {"name": "report_api", "value": "", "description": "d", "required": True}
    assert [v["name"] for v in client.get("/api/case/variables").json()] == ["report_api", "engagement"]
    assert client.delete("/api/case/variables/engagement").json() == {"ok": True}
    assert [v["name"] for v in client.get("/api/case/variables").json()] == ["report_api"]


def test_route_rejects_bad_names(client):
    r = client.post("/api/case/variables", json={"name": "not ok", "value": "v"})
    assert r.status_code == 400


def test_route_needs_the_csrf_header(client):
    from fastapi.testclient import TestClient
    import server
    bare = TestClient(server.app)
    assert bare.post("/api/case/variables", json={"name": "a", "value": "b"}).status_code == 403


# --------------------------------------------------------- profiles / apply

def test_bundles_store_variable_definitions():
    pb = PluginBundles()  # the autouse fixture points workspace/ at a tmp dir
    rec = pb.save("Triage", ["lateral_movement"],
                  variables=[{"name": "engagement", "required": True, "label": "Engagement"}])
    assert rec["variables"] == [{"name": "engagement", "required": True, "label": "Engagement"}]
    # re-saving without variables keeps the definitions; with them, replaces
    rec2 = pb.save("Triage", ["lateral_movement"])
    assert rec2["variables"] == rec["variables"]
    rec3 = pb.save("Triage", ["lateral_movement"], variables=[])
    assert rec3["variables"] == []
    assert PluginBundles().get(rec["id"])["variables"] == []


def test_shipped_profiles_declare_variables_with_valid_names():
    from winnow import defaults
    from winnow.store import Store
    assert any(prof.get("variables") for prof in defaults.profiles()), "the shipped example is gone"
    for prof in defaults.profiles():
        for d in prof.get("variables", []):
            assert Store.VARIABLE_NAME_RE.match(d["name"]), (prof["name"], d)
            assert not d.get("required"), "shipped profiles must not gate case creation"


def test_apply_seeds_variables_and_reports_required_gaps(client, store):
    r = client.post("/api/plugin_bundles", json={
        "name": "Vars", "plugins": [],
        "variables": [{"name": "engagement", "required": True},
                      {"name": "doc_link", "default": "https://docs.local"}]})
    assert r.status_code == 200
    bid = r.json()["id"]
    store.set_variable("doc_link", "kept")
    res = client.post(f"/api/plugin_bundles/{bid}/apply").json()
    assert res["variables_missing"] == ["engagement"]
    assert store.get_variables() == {"engagement": "", "doc_link": "kept"}
    store.set_variable("engagement", "ACME")
    assert client.post(f"/api/plugin_bundles/{bid}/apply").json()["variables_missing"] == []


def test_save_route_accepts_variables(client):
    r = client.post("/api/plugin_bundles", json={
        "name": "V2", "plugins": [], "variables": [{"name": "x", "label": "X"}]})
    assert r.json()["variables"] == [{"name": "x", "label": "X"}]
    listed = next(b for b in client.get("/api/plugin_bundles").json() if b["id"] == r.json()["id"])
    assert listed["variables"] == [{"name": "x", "label": "X"}]


# ------------------------------------------------------------ PluginRequest

def _req(store, body=None):
    return plugin_api.PluginRequest("POST", "x", {}, body, store, {})


def test_plugin_request_reads_and_writes_variables(store):
    store.set_variable("engagement", "ACME")
    req = _req(store)
    assert req.variables == {"engagement": "ACME"}
    req.set_variable("last_run", "2026-09-02")
    assert store.get_variables()["last_run"] == "2026-09-02"
    with pytest.raises(ValueError):
        req.set_variable("bad name", "x")


def test_plugin_request_without_a_case():
    req = _req(None)
    assert req.variables == {}
    with pytest.raises(ValueError):
        req.set_variable("a", "b")


def test_plugin_route_sees_case_variables(client, store, tmp_path, monkeypatch):
    """End to end: a plugin API route reads a variable the analyst set."""
    import textwrap
    import server
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "varsdemo.py").write_text(textwrap.dedent("""
        PLUGIN = {"name": "varsdemo", "version": "0.1", "description": "reads a case variable"}
        def register(api):
            api.register_api("engagement", lambda req: {"engagement": req.variables.get("engagement"),
                                                         "set": req.set_variable("touched", "yes")["value"]})
    """))
    reg = plugin_api.PluginRegistry()
    reg.load([pdir])
    monkeypatch.setattr(server, "PLUGINS", reg)
    store.set_variable("engagement", "ACME")
    r = client.get("/api/plugin/varsdemo/engagement")
    assert r.status_code == 200, r.text
    assert r.json() == {"engagement": "ACME", "set": "yes"}
    assert store.get_variables()["touched"] == "yes"
