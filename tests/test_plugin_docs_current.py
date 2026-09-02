"""The plugin authoring surface and its documentation move together: every
public register_* hook on PluginAPI must be described in the module
docstring (the contract) and in docs/writing-plugins.md (the guide and
its Reference section). A hook without docs fails here, not in a user's
plugin folder."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from winnow import plugin_api

ROOT = Path(__file__).resolve().parent.parent
GUIDE = (ROOT / "docs" / "writing-plugins.md").read_text(encoding="utf-8")


def _hooks():
    return sorted(n for n, _ in inspect.getmembers(plugin_api.PluginAPI, inspect.isfunction)
                  if n.startswith("register_"))


def test_there_are_hooks():
    assert len(_hooks()) >= 4


def test_every_hook_is_in_the_module_docstring():
    doc = plugin_api.__doc__ or ""
    for name in _hooks():
        assert f"api.{name}(" in doc, f"{name} is missing from plugin_api's module docstring"


def test_every_hook_is_in_the_guide_and_its_reference():
    for name in _hooks():
        assert f"api.{name}(" in GUIDE, f"{name} is not in docs/writing-plugins.md"
        assert re.search(rf"^### `{name}\(", GUIDE, re.M), f"{name} has no Reference entry in docs/writing-plugins.md"


def test_guide_extension_point_count_matches():
    n = len(_hooks())
    words = {3: "three", 4: "four", 5: "five", 6: "six"}
    assert f"## 1. The {words[n]} extension points" in GUIDE


def test_api_version_is_noted_in_the_guide():
    assert f"current plugin API version is **{plugin_api.PLUGIN_API_VERSION}**" in GUIDE
