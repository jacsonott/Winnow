"""register_page_panel: a plugin's panel beside the SQL or Notes page.

The registrar validates like register_toolbar_panel (folder plugins, an
entry that exists now, an id in the shared tab/panel namespace) plus the
page name, and the listing carries the gen the frontend cache-busts
with. The frontend half is tests/ui/test_plugin_page_panels.py.
"""
import textwrap
from pathlib import Path

import pytest

from winnow.plugin_api import PAGE_PANEL_PAGES, PluginRegistry

PANEL_PLUGIN = """
    def register(api):
        api.register_tab(id="view", label="My View", entry="ui/tab.js")
        api.register_page_panel(page="sql", id="helper", label="Helper", entry="ui/panel.js",
                                description="a SQL helper")
        api.register_page_panel(page="notes", id="drafts", label="Drafts", entry="ui/panel.js")
"""


def _folder_plugin(root: Path, name: str, init: str) -> Path:
    d = root / name
    (d / "ui").mkdir(parents=True)
    (d / "__init__.py").write_text(textwrap.dedent(init))
    (d / "ui" / "tab.js").write_text("export default function mount(c, w) {}\n")
    (d / "ui" / "panel.js").write_text("export default function mount(c, w) {}\n")
    return d


@pytest.fixture
def panel_dir(tmp_path) -> Path:
    d = tmp_path / "plugs"
    _folder_plugin(d, "pp", PANEL_PLUGIN)
    return d


@pytest.fixture
def panel_registry(panel_dir) -> PluginRegistry:
    reg = PluginRegistry()
    reg.load([panel_dir])
    (rec,) = reg.describe()
    assert rec["error"] is None, rec["error"]
    return reg


def test_pages_are_sql_and_notes():
    assert PAGE_PANEL_PAGES == ("sql", "notes")


def test_page_panels_are_listed_with_gen(panel_registry):
    panels = {p["id"]: p for p in panel_registry.list_page_panels()}
    assert set(panels) == {"pp.helper", "pp.drafts"}
    h = panels["pp.helper"]
    assert h["page"] == "sql" and h["local_id"] == "helper" and h["label"] == "Helper"
    assert h["entry"] == "ui/panel.js" and h["plugin_fs"] == "pp" and h["description"] == "a SQL helper"
    assert h["gen"] > 0
    assert panels["pp.drafts"]["page"] == "notes"
    # Not mixed into the toolbar panels or the tabs
    assert panel_registry.list_panels() == []
    assert [t["id"] for t in panel_registry.list_tabs()] == ["pp.view"]


def test_gen_changes_on_reload(panel_dir, panel_registry):
    old = panel_registry.list_page_panels()[0]["gen"]
    panel_registry.load([panel_dir])
    assert panel_registry.list_page_panels()[0]["gen"] != old


def test_listing_route_carries_page_panels(client, panel_registry, panel_dir, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", panel_registry)
    monkeypatch.setattr(server, "PLUGIN_DIRS", [panel_dir])
    r = client.get("/api/plugins").json()
    assert sorted(p["id"] for p in r["page_panels"]) == ["pp.drafts", "pp.helper"]
    assert r["api_version"] >= 9


def test_disabled_plugin_has_no_page_panels(client, panel_registry, panel_dir, monkeypatch):
    import server

    monkeypatch.setattr(server, "PLUGINS", panel_registry)
    monkeypatch.setattr(server, "PLUGIN_DIRS", [panel_dir])
    client.post("/api/plugins/toggle", json={"fs_name": "pp", "scope": "off_all"})
    assert client.get("/api/plugins").json()["page_panels"] == []


@pytest.mark.parametrize("name, init, expect", [
    ("badpage", """
        def register(api):
            api.register_page_panel(page="grid", id="x", label="X", entry="ui/panel.js")
     """, "must be one of sql, notes"),
    ("badid", """
        def register(api):
            api.register_page_panel(page="sql", id="Not OK", label="X", entry="ui/panel.js")
     """, "lowercase"),
    ("nolabel", """
        def register(api):
            api.register_page_panel(page="sql", id="x", label="", entry="ui/panel.js")
     """, "needs a label"),
    ("missing", """
        def register(api):
            api.register_page_panel(page="sql", id="x", label="X", entry="ui/nope.js")
     """, "inside the plugin folder"),
    ("dupe_with_tab", """
        def register(api):
            api.register_tab(id="copilot", label="C", entry="ui/tab.js")
            api.register_page_panel(page="sql", id="copilot", label="C", entry="ui/panel.js")
     """, "share one id space"),
    ("dupe_with_toolbar", """
        def register(api):
            api.register_toolbar_panel(id="h", label="H", entry="ui/panel.js")
            api.register_page_panel(page="notes", id="h", label="H", entry="ui/panel.js")
     """, "share one id space"),
])
def test_registration_validation(tmp_path, name, init, expect):
    d = tmp_path / "plugs"
    _folder_plugin(d, name, init)
    reg = PluginRegistry()
    reg.load([d])
    (rec,) = reg.describe()
    assert rec["error"] and expect in rec["error"], rec["error"]


def test_single_file_plugin_cannot_register_one(tmp_path):
    d = tmp_path / "plugs"
    d.mkdir()
    (d / "solo.py").write_text(textwrap.dedent("""
        def register(api):
            api.register_page_panel(page="sql", id="x", label="X", entry="panel.js")
    """))
    reg = PluginRegistry()
    reg.load([d])
    (rec,) = reg.describe()
    assert "folder plugins" in rec["error"]


def test_a_tab_cannot_reuse_a_page_panel_id_either(tmp_path):
    """The check is symmetric: whichever hook registers second loses."""
    d = tmp_path / "plugs"
    _folder_plugin(d, "rev", """
        def register(api):
            api.register_page_panel(page="sql", id="copilot", label="C", entry="ui/panel.js")
            api.register_tab(id="copilot", label="C", entry="ui/tab.js")
    """)
    reg = PluginRegistry()
    reg.load([d])
    (rec,) = reg.describe()
    assert "Duplicate tab id" in rec["error"] and "page panel" in rec["error"]
