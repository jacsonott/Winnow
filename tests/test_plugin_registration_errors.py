"""What a plugin author is told when a register_* call is wrong.

register_ingest_format, register_tab, register_row_action and
register_toolbar_panel each refuse bad arguments with a message, and the
message is a plugin author's first contact with the API: it is what
Settings → Plugins shows against the plugin. None of those branches had
run. Each case here registers something valid first and then makes the one
mistake, so it also holds the other half of the promise: a register() that
fails keeps none of what it registered (the registry's rollback), rather
than a plugin half there with an error beside it.

register_page_panel's and register_dashboard's own refusals are in
tests/test_plugin_page_panels.py and tests/test_plugin_dashboards.py.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from winnow.plugin_api import OPTION_TYPES, PluginRegistry

# Registered before the mistake in every case, one of each kind the four
# hooks make, so a rollback that missed a table would leave it listed.
VALID_FIRST = """
def register(api):
    api.register_ingest_format(id="first", label="First", parse=lambda *a, **k: iter(()))
    api.register_tab(id="firsttab", label="First tab", entry="ui/tab.js")
    api.register_row_action(id="firstaction", label="First action", handler=lambda req: {})
    api.register_toolbar_panel(id="firstpanel", label="First panel", entry="ui/panel.js")
"""

PARSE = "parse=lambda *a, **k: iter(())"
HANDLER = "handler=lambda req: {}"

CASES = [
    # register_ingest_format
    ("format_id", f'api.register_ingest_format(id="Bad Id", label="X", {PARSE})',
     "Format id 'Bad Id' must be lowercase [a-z0-9_-]"),
    ("format_no_id", f'api.register_ingest_format(id="", label="X", {PARSE})',
     "Format id '' must be lowercase [a-z0-9_-]"),
    ("format_label", f'api.register_ingest_format(id="x", label="", {PARSE})',
     "register_ingest_format needs a label and a callable parse"),
    ("format_parse", 'api.register_ingest_format(id="x", label="X", parse="not callable")',
     "register_ingest_format needs a label and a callable parse"),
    ("format_option_dict", f'api.register_ingest_format(id="x", label="X", {PARSE}, options=["delim"])',
     "Each option must be a dict with a 'name'"),
    ("format_option_name", f'api.register_ingest_format(id="x", label="X", {PARSE}, options=[{{"type": "text"}}])',
     "Each option must be a dict with a 'name'"),
    ("format_option_type",
     f'api.register_ingest_format(id="x", label="X", {PARSE}, options=[{{"name": "n", "type": "slider"}}])',
     f"Option 'n': type must be one of {sorted(OPTION_TYPES)}"),
    ("format_choice",
     f'api.register_ingest_format(id="x", label="X", {PARSE}, options=[{{"name": "mode", "type": "choice"}}])',
     "Option 'mode': choice type needs 'choices'"),
    # register_tab
    ("tab_id", 'api.register_tab(id="Tab!", label="X", entry="ui/tab.js")',
     "Tab id 'Tab!' must be lowercase [a-z0-9_-]"),
    ("tab_label", 'api.register_tab(id="x", label="", entry="ui/tab.js")',
     "register_tab needs a label"),
    ("tab_missing", 'api.register_tab(id="x", label="X", entry="ui/nope.js")',
     "Tab entry 'ui/nope.js' must be a file inside the plugin folder"),
    ("tab_escape", 'api.register_tab(id="x", label="X", entry="../outside.js")',
     "Tab entry '../outside.js' must be a file inside the plugin folder"),
    ("tab_absolute", 'api.register_tab(id="x", label="X", entry="/etc/passwd")',
     "Tab entry '/etc/passwd' must be a file inside the plugin folder"),
    ("tab_a_folder", 'api.register_tab(id="x", label="X", entry="ui")',
     "Tab entry 'ui' must be a file inside the plugin folder"),
    # register_row_action
    ("action_id", f'api.register_row_action(id="Look Up", label="X", {HANDLER})',
     "Row action id 'Look Up' must be lowercase [a-z0-9_-]"),
    ("action_label", f'api.register_row_action(id="x", label="", {HANDLER})',
     "register_row_action needs a label and a callable handler"),
    ("action_handler", 'api.register_row_action(id="x", label="X", handler=None)',
     "register_row_action needs a label and a callable handler"),
    ("action_max_rows_text", f'api.register_row_action(id="x", label="X", {HANDLER}, max_rows="many")',
     "max_rows must be an integer"),
    ("action_max_rows_none", f'api.register_row_action(id="x", label="X", {HANDLER}, max_rows=None)',
     "max_rows must be an integer"),
    ("action_max_rows_zero", f'api.register_row_action(id="x", label="X", {HANDLER}, max_rows=0)',
     "max_rows must be at least 1"),
    # register_toolbar_panel
    ("panel_id", 'api.register_toolbar_panel(id="UPPER", label="X", entry="ui/panel.js")',
     "Panel id 'UPPER' must be lowercase [a-z0-9_-]"),
    ("panel_label", 'api.register_toolbar_panel(id="x", label="", entry="ui/panel.js")',
     "register_toolbar_panel needs a label"),
    ("panel_missing", 'api.register_toolbar_panel(id="x", label="X", entry="ui/nope.js")',
     "Panel entry 'ui/nope.js' must be a file inside the plugin folder"),
    ("panel_escape", 'api.register_toolbar_panel(id="x", label="X", entry="ui/../../outside.js")',
     "Panel entry 'ui/../../outside.js' must be a file inside the plugin folder"),
]


def _folder_plugin(root: Path, name: str, mistake: str) -> Path:
    d = root / name
    (d / "ui").mkdir(parents=True)
    (d / "__init__.py").write_text(textwrap.dedent(VALID_FIRST) + f"    {mistake}\n")
    (d / "ui" / "tab.js").write_text("export default function mount(c, w) {}\n")
    (d / "ui" / "panel.js").write_text("export function mount(c, w) {}\n")
    (root / "outside.js").write_text("export function mount(c, w) {}\n")
    return d


def _load(directory: Path) -> tuple[PluginRegistry, dict]:
    reg = PluginRegistry()
    reg.load([directory])
    (rec,) = reg.describe()
    return reg, rec


def _nothing_kept(reg: PluginRegistry) -> None:
    assert reg.list_formats() == []
    assert reg.list_tabs() == []
    assert reg.list_row_actions() == []
    assert reg.list_panels() == []


def test_the_valid_registrations_alone_load(tmp_path):
    """The baseline every case below adds one mistake to."""
    d = tmp_path / "plugs"
    _folder_plugin(d, "fine", "pass")
    reg, rec = _load(d)
    assert rec["error"] is None, rec["error"]
    assert [f["id"] for f in reg.list_formats()] == ["fine.first"]
    assert [t["id"] for t in reg.list_tabs()] == ["fine.firsttab"]
    assert [a["id"] for a in reg.list_row_actions()] == ["fine.firstaction"]
    assert [p["id"] for p in reg.list_panels()] == ["fine.firstpanel"]


@pytest.mark.parametrize("name, mistake, message", CASES, ids=[c[0] for c in CASES])
def test_a_bad_registration_says_what_is_wrong_and_keeps_nothing(tmp_path, name, mistake, message):
    d = tmp_path / "plugs"
    _folder_plugin(d, name, mistake)
    reg, rec = _load(d)
    assert rec["error"] == f"ValueError: {message}"
    _nothing_kept(reg)


@pytest.mark.parametrize("hook, call", [
    ("register_tab", 'api.register_tab(id="x", label="X", entry="tab.js")'),
    ("register_toolbar_panel", 'api.register_toolbar_panel(id="x", label="X", entry="panel.js")'),
])
def test_a_single_file_plugin_is_told_ui_needs_a_folder(tmp_path, hook, call):
    """A plugin that is one .py file has nowhere to keep a module to serve,
    and is told so, rather than that its entry is missing."""
    d = tmp_path / "plugs"
    d.mkdir()
    (d / "solo.py").write_text(textwrap.dedent(f"""
        def register(api):
            api.register_ingest_format(id="first", label="First", parse=lambda *a, **k: iter(()))
            {call}
    """))
    (d / "tab.js").write_text("export default function mount(c, w) {}\n")
    (d / "panel.js").write_text("export function mount(c, w) {}\n")
    reg, rec = _load(d)
    assert rec["error"] == (f"ValueError: {hook} is for folder plugins — a single .py file has "
                            f"no {'assets' if hook == 'register_tab' else 'module'} to serve")
    assert reg.list_formats() == []
