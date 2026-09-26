"""A plugin describes itself without being imported.

A plugin that is switched off is never imported — that is the whole value
of an off switch on something that runs with the app's privileges — so
until `static_meta` existed, the listing had nothing to show for one but
its folder name. These pin the two halves of that: the metadata really is
read from the file, and reading it really does not run the file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from winnow.plugin_api import PluginRegistry, static_meta  # noqa: E402

BUNDLED = Path(__file__).resolve().parent.parent / "examples" / "plugins"


def write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / f"{name}.py"
    p.write_text(body, encoding="utf-8")
    return p


# ------------------------------------------------------------ reading it

def test_reads_name_version_description_without_importing(tmp_path):
    entry = write(tmp_path, "demo", '''
PLUGIN = {"name": "demo-plugin", "version": "2.1", "description": "Does a thing."}
WINNOW_API_VERSION = 4

def register(api):
    api.register_tab(id="x", label="X", entry="ui/x.js")
''')
    m = static_meta(entry)
    assert m["name"] == "demo-plugin"
    assert m["version"] == "2.1"
    assert m["description"] == "Does a thing."
    assert m["api_wants"] == 4
    assert m["declares"] == ["tab"]


def test_reading_it_does_not_execute_the_module(tmp_path):
    """The point of the whole exercise. If this file were imported the
    sentinel would exist and the RuntimeError would escape."""
    sentinel = tmp_path / "ran"
    entry = write(tmp_path, "hostile", f'''
from pathlib import Path
Path({str(sentinel)!r}).write_text("executed")
raise RuntimeError("import-time explosion")

PLUGIN = {{"name": "hostile", "description": "never runs"}}
''')
    m = static_meta(entry)
    assert not sentinel.exists(), "static_meta imported the module"
    # The assignment is unreachable at runtime but present in the source,
    # which is exactly the difference between reading and running.
    assert m["name"] == "hostile"


def test_declares_lists_kinds_not_counts(tmp_path):
    """Three register_ingest_format calls are one kind, not three. A count
    read out of source is a guess the moment one sits inside a loop."""
    entry = write(tmp_path, "many", '''
def register(api):
    api.register_ingest_format(id="a", label="A", parse=None)
    api.register_ingest_format(id="b", label="B", parse=None)
    for n in ("c", "d"):
        api.register_ingest_format(id=n, label=n, parse=None)
    api.register_tab(id="t", label="T", entry="ui/t.js")
''')
    assert static_meta(entry)["declares"] == ["ingest_format", "tab"]


def test_bare_call_is_not_ours(tmp_path):
    """`register_tab(...)` with no receiver is somebody else's function —
    the name only exists as a method on the PluginAPI that register() is
    handed."""
    entry = write(tmp_path, "bare", '''
from elsewhere import register_tab
register_tab("not ours")
''')
    assert static_meta(entry)["declares"] == []


# --------------------------------------------------- when it can't be read

@pytest.mark.parametrize("body,why", [
    ("def register(api:\n", "does not parse"),
    ("PLUGIN = 'a string, not a dict'", "PLUGIN is the wrong type"),
    ("PLUGIN = {'name': some_variable}", "value is not a literal"),
    ("WINNOW_API_VERSION = 'nine'", "version is not an int"),
    ("", "empty file"),
])
def test_unreadable_metadata_is_empty_never_an_exception(tmp_path, body, why):
    m = static_meta(write(tmp_path, "bad", body))
    assert m["name"] is None, why
    assert m["version"] is None
    assert m["description"] == ""
    assert m["api_wants"] is None
    assert m["declares"] == []


def test_missing_file_is_empty(tmp_path):
    assert static_meta(tmp_path / "nope.py")["name"] is None


def test_true_is_not_an_api_version(tmp_path):
    """bool is a subclass of int, and `WINNOW_API_VERSION = True` meaning
    "v1" is a coincidence nobody should ship."""
    assert static_meta(write(tmp_path, "b", "WINNOW_API_VERSION = True"))["api_wants"] is None


def test_last_assignment_wins(tmp_path):
    entry = write(tmp_path, "twice", '''
PLUGIN = {"name": "first"}
PLUGIN = {"name": "second"}
''')
    assert static_meta(entry)["name"] == "second"


# ------------------------------------------------- what the listing shows

def test_disabled_plugin_still_names_itself(tmp_path):
    """The bug this whole change exists for: a switched-off plugin listed
    as its folder name with no version and no description."""
    reg = PluginRegistry()
    reg.load([str(BUNDLED)], bundled_dirs=[str(BUNDLED)], enabled_for=lambda f, d: False)
    by_fs = {p["fs_name"]: p for p in reg.describe()}
    assert by_fs, "no bundled examples found"
    for fs, p in by_fs.items():
        assert p["enabled"] is False
        assert p["name"] != fs, f"{fs} is still listed by its folder name"
        assert p["version"], f"{fs} has no version"
        assert p["description"], f"{fs} has no description"
        assert p["entry"] == "__init__.py"
        assert p["declares"], f"{fs} declares nothing"


def test_enabled_plugin_prefers_the_registry_over_the_source(tmp_path):
    """A loaded plugin has real registry data; `declares` is the answer for
    the one that was deliberately not run, and must not pretend otherwise."""
    reg = PluginRegistry()
    reg.load([str(BUNDLED)], bundled_dirs=[str(BUNDLED)],
             enabled_for=lambda f, d: f == "mft_usn")
    rec = next(p for p in reg.describe() if p["fs_name"] == "mft_usn")
    assert rec["enabled"] is True
    assert rec["declares"] == []
    # The registry's own count — two formats, from a folder whose SOURCE
    # contains three register_ingest_format calls (one lives in a helper
    # module that the entry file never reaches). Exactly the arithmetic
    # that makes a source-read count a guess and a source-read kind a
    # fact, which is why `declares` carries kinds.
    assert sorted(rec["formats"]) == ["mft-usn.mft", "mft-usn.usn"]
    assert rec["name"] == "mft-usn"
    assert rec["api_wants"] == 1


def test_a_plugin_that_fails_to_load_keeps_its_name(tmp_path):
    """The moment a plugin is least identifiable is the moment it most
    needs to be: an import that dies on line one used to leave the listing
    holding a folder name and a traceback."""
    d = tmp_path / "broken"
    d.mkdir()
    (d / "__init__.py").write_text('''
PLUGIN = {"name": "broken-thing", "version": "1.0", "description": "Explodes on import."}
WINNOW_API_VERSION = 1
raise RuntimeError("boom")

def register(api):
    api.register_tab(id="t", label="T", entry="ui/t.js")
''', encoding="utf-8")
    reg = PluginRegistry()
    reg.load([str(tmp_path)], enabled_for=lambda f, dd: True)
    rec = next(p for p in reg.describe() if p["fs_name"] == "broken")
    assert rec["error"] and "boom" in rec["error"]
    assert rec["name"] == "broken-thing"
    assert rec["version"] == "1.0"
    assert rec["description"] == "Explodes on import."
    assert rec["declares"] == ["tab"]


def test_readme_is_reported_by_name_only(tmp_path):
    d = tmp_path / "withdoc"
    d.mkdir()
    (d / "__init__.py").write_text("def register(api):\n    pass\n", encoding="utf-8")
    (d / "README.md").write_text("# hi", encoding="utf-8")
    reg = PluginRegistry()
    reg.load([str(tmp_path)], enabled_for=lambda f, dd: False)
    rec = next(p for p in reg.describe() if p["fs_name"] == "withdoc")
    assert rec["readme"] == "README.md"


def test_single_file_plugin_has_no_readme(tmp_path):
    (tmp_path / "solo.py").write_text("def register(api):\n    pass\n", encoding="utf-8")
    reg = PluginRegistry()
    reg.load([str(tmp_path)], enabled_for=lambda f, dd: False)
    rec = next(p for p in reg.describe() if p["fs_name"] == "solo")
    assert rec["readme"] is None
    assert rec["entry"] == "solo.py"
