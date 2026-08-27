"""Plugin bundles — named per-machine plugin sets ("case types") and the
one-shot apply that sets the open case's overrides to exactly the bundle."""

from __future__ import annotations

from pathlib import Path

import pytest

import workspace as WS
from plugin_api import PluginRegistry

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"


@pytest.fixture
def registry(monkeypatch):
    import server

    reg = PluginRegistry()
    reg.load([EXAMPLES])
    monkeypatch.setattr(server, "PLUGINS", reg)
    # apply() ends in _reload_plugins(), which rescans PLUGIN_DIRS — pin
    # those to the examples too, or the machine's real plugins leak into
    # the response with no override.
    monkeypatch.setattr(server, "PLUGIN_DIRS", [EXAMPLES])
    monkeypatch.setattr(server, "BUNDLED_PLUGIN_DIR", EXAMPLES)
    return reg


def test_bundles_upsert_by_name_and_delete(client):
    r = client.post("/api/plugin_bundles", json={"name": "Triage", "plugins": ["lateral_movement", "pivot"]})
    assert r.status_code == 200
    bid = r.json()["id"]
    # same name = replace, not duplicate
    r = client.post("/api/plugin_bundles", json={"name": "triage", "plugins": ["pivot"]})
    assert r.json()["id"] == bid
    listed = client.get("/api/plugin_bundles").json()
    assert [(b["name"], b["plugins"]) for b in listed if b["id"] == bid] == [("Triage", ["pivot"])]
    assert client.delete(f"/api/plugin_bundles/{bid}").status_code == 200
    assert all(b["id"] != bid for b in client.get("/api/plugin_bundles").json())


def test_bundle_rejects_blank_names(client):
    assert client.post("/api/plugin_bundles", json={"name": "  ", "plugins": []}).status_code == 400


def test_apply_sets_case_overrides_to_exactly_the_bundle(client, store, registry):
    bid = client.post("/api/plugin_bundles",
                      json={"name": "T", "plugins": ["lateral_movement", "not_installed_here"]}).json()["id"]
    r = client.post(f"/api/plugin_bundles/{bid}/apply")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["enabled"] == ["lateral_movement"]
    assert out["missing"] == ["not_installed_here"]
    # every installed plugin got an explicit override: bundle members on,
    # everything else off — the case's plugin set IS the bundle.
    plugins = out["plugins"]["plugins"]
    for p in plugins:
        assert p["case_override"] == (p["fs_name"] == "lateral_movement"), p["fs_name"]
    lat = next(p for p in plugins if p["fs_name"] == "lateral_movement")
    assert lat["enabled"] is True


def test_apply_without_a_case_is_a_400(client, registry, monkeypatch):
    import server

    bid = client.post("/api/plugin_bundles", json={"name": "X", "plugins": []}).json()["id"]
    monkeypatch.setattr(server, "STORE", None)
    assert client.post(f"/api/plugin_bundles/{bid}/apply").status_code == 400
