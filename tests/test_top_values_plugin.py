"""The top_values example — the reference for register_toolbar_panel now
that the histogram is built in: it registers a panel and nothing else,
the listing carries it only while the plugin is enabled, and the hook's
own validation (an entry outside the plugin folder) still holds."""

from __future__ import annotations

import json
from pathlib import Path

from winnow import plugin_api
from winnow.plugin_api import PluginRegistry

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"
PANEL_ID = "top-values.top_values"


def _load(enabled_for=None):
    reg = PluginRegistry()
    reg.load([EXAMPLES], enabled_for=enabled_for)
    rec = next(p for p in reg.describe() if p["fs_name"] == "top_values")
    return reg, rec


def test_example_registers_a_toolbar_panel_and_nothing_else(client, monkeypatch):
    import server
    reg, rec = _load()
    assert rec["error"] is None, rec["error"]
    assert rec["enabled"] and rec["bundled"] is False   # bundled is a server-level flag; the loader alone says no
    monkeypatch.setattr(server, "PLUGINS", reg)
    r = client.get("/api/plugins").json()
    (panel,) = [p for p in r["panels"] if p["id"] == PANEL_ID]
    assert panel["entry"] == "ui/panel.js" and panel["label"] == "Top values" and panel["plugin_fs"] == "top_values"
    assert (EXAMPLES / "top_values" / panel["entry"]).is_file()
    # No routes, tabs, formats, row actions, page panels or dashboards: the
    # counts come from the app's own /api/group_summary.
    assert rec["tabs"] == [] and rec["formats"] == []
    for kind in ("tabs", "formats", "row_actions", "page_panels", "dashboards"):
        assert "top_values" not in json.dumps(r.get(kind, [])), kind
    assert reg.get_api("top_values", "anything") is None


def test_the_example_is_written_against_the_current_api():
    """The reference example should never ask for an API older than the
    one it ships with — a reader copies the number."""
    text = (EXAMPLES / "top_values" / "__init__.py").read_text(encoding="utf-8")
    assert f"WINNOW_API_VERSION = {plugin_api.PLUGIN_API_VERSION}\n" in text


def test_a_disabled_example_has_no_panel(client, monkeypatch):
    import server
    reg, rec = _load(enabled_for=lambda fs_name, directory: fs_name != "top_values")
    assert rec["enabled"] is False and rec["error"] is None
    monkeypatch.setattr(server, "PLUGINS", reg)
    r = client.get("/api/plugins").json()
    assert PANEL_ID not in [p["id"] for p in r["panels"]]


def test_panel_registration_validates(tmp_path):
    d = tmp_path / "badpanel"
    d.mkdir()
    (d / "__init__.py").write_text(
        "def register(api):\n    api.register_toolbar_panel(id='p', label='x', entry='ui/missing.js')\n")
    reg = PluginRegistry()
    reg.load([tmp_path])
    rec = next(p for p in reg.describe() if p["fs_name"] == "badpanel")
    assert rec["error"] and "inside the plugin folder" in rec["error"]
