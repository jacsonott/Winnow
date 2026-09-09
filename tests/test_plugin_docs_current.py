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


def _request_members():
    """Public data members and methods a handler can reach on PluginRequest
    (what __init__ assigns, plus properties and methods)."""
    names = set()
    src = inspect.getsource(plugin_api.PluginRequest.__init__)
    names.update(re.findall(r"self\.([a-z_]+)\s*=", src))
    for n, _ in inspect.getmembers(plugin_api.PluginRequest):
        if not n.startswith("_"):
            names.add(n)
    return sorted(names)


def test_every_pluginrequest_member_is_in_the_guide():
    """Adding a field or method to PluginRequest means documenting it — in
    the module docstring's handler example and in the guide's Reference."""
    assert "### `PluginRequest`" in GUIDE
    ref = GUIDE.split("### `PluginRequest`", 1)[1].split("\n### ", 1)[0]
    doc = plugin_api.__doc__ or ""
    for name in _request_members():
        assert f"`{name}`" in ref or f"`{name}(" in ref, f"PluginRequest.{name} has no Reference row"
        assert re.search(rf"\b{name}\b", doc), f"PluginRequest.{name} is not in plugin_api's module docstring"


# ------------------------------------------------ the tab/panel `winnow` context

PLUGINS_JS = (ROOT / "static" / "js" / "plugins.js").read_text(encoding="utf-8")


def _tab_context_members():
    """The keys of the object buildPluginTabContext() returns, plus the
    getters on its `state`.

    Read out of the JS by shape rather than parsed: the object is a literal
    with one member per line, which is also how it stays readable. If that
    stops being true this returns too few members and
    test_the_context_scan_sees_the_object fails, rather than the whole
    check quietly passing on an empty set."""
    body = PLUGINS_JS.split("export function buildPluginTabContext", 1)[1]
    body = body.split("\n}", 1)[0]

    top, state = set(), set()
    in_state = False
    for line in body.splitlines():
        if re.match(r"^    state: \{", line):
            in_state = True
            top.add("state")
            continue
        if in_state:
            if re.match(r"^    \},?$", line):
                in_state = False
                continue
            m = re.match(r"^      get (\w+)\(", line)
            if m:
                state.add(m.group(1))
            continue
        m = re.match(r"^    (\w+):", line)          # name: value
        if m:
            top.add(m.group(1))
            continue
        m = re.match(r"^    (\w+(?:, \w+)*),$", line)   # shorthand list
        if m:
            top.update(n.strip() for n in m.group(1).split(","))
    return top, state


def test_the_context_scan_sees_the_object():
    """A scan that matches nothing passes forever."""
    top, state = _tab_context_members()
    assert len(top) >= 15, f"only found {sorted(top)} — has the context's shape changed?"
    assert {"api", "post", "el", "state"} <= top
    assert {"sources", "sourceId"} <= state, sorted(state)


def test_every_context_member_is_in_the_guide():
    """A field a plugin author cannot discover may as well not exist —
    `openFiltered` sat undocumented through three releases."""
    top, state = _tab_context_members()
    guide = GUIDE
    # `state` is the container; its members are checked below, and the guide
    # documents them as `state.sources` rather than naming the object.
    missing = [n for n in sorted(top - {"state"})
               if f"`{n}`" not in guide and f"`{n}(" not in guide]
    assert not missing, f"undocumented on the winnow context: {missing}"
    missing_state = [n for n in sorted(state)
                     if f"`state.{n}`" not in guide and f"state.{n}" not in guide]
    assert not missing_state, f"undocumented on winnow.state: {missing_state}"


def test_the_context_api_version_matches_the_code():
    m = re.search(r"apiVersion: (\d+)", PLUGINS_JS)
    assert m, "buildPluginTabContext no longer declares an apiVersion"
    assert f"Contract version of this object (currently `{m.group(1)}`)" in GUIDE, (
        f"the guide's context apiVersion disagrees with plugins.js ({m.group(1)})")


# ------------------------------------------------------ the guide's own shape

def _headings():
    return re.findall(r"^## (\d+)\. (.+)$", GUIDE, re.M)


def _anchor(title: str) -> str:
    a = re.sub(r"[^\w\s-]", "", title.lower())
    return re.sub(r"\s+", "-", a.strip())


def test_the_contents_match_the_sections():
    """The table of contents said "three extension points" and linked
    #1-the-three-extension-points long after there were five, and its
    numbering drifted a whole section out from §6 on — every link below
    that point went to the wrong place, or nowhere."""
    heads = _headings()
    assert len(heads) >= 10
    listed = re.findall(r"^(\d+)\. \[([^\]]+)\]\(#([^)]+)\)$", GUIDE, re.M)
    listed = listed[:len(heads)]
    assert len(listed) == len(heads), (
        f"{len(listed)} contents entries for {len(heads)} sections")
    for (hn, htitle), (ln, ltitle, lanchor) in zip(heads, listed):
        assert hn == ln and htitle == ltitle, f"contents says {ln}. {ltitle}, section is {hn}. {htitle}"
        assert lanchor == _anchor(f"{hn}. {htitle}"), f"broken anchor for {htitle}: #{lanchor}"


def test_every_bundled_example_is_listed():
    """A reference implementation nobody can find is not one — three of the
    seven examples were missing from the table."""
    examples = sorted(d.name for d in (ROOT / "examples" / "plugins").iterdir()
                      if d.is_dir() and not d.name.startswith("__"))
    missing = [e for e in examples if f"[`{e}/`]" not in GUIDE]
    assert not missing, f"bundled examples missing from the guide: {missing}"


def test_section_cross_references_point_where_they_say():
    """§N references drifted with the numbering: the LLM section told
    readers to use "the standalone recipe in §8" (row actions) and to
    cross-check API calls against "§12" (the security model). A prompt
    built from those sends an LLM to the wrong page of the contract."""
    heads = {int(n): t for n, t in _headings()}
    expected = {
        "recipe in §": "Testing a plugin",
        "Cross-check any API call against §": "Reference",
    }
    for phrase, want in expected.items():
        for m in re.finditer(re.escape(phrase) + r"(\d+)", GUIDE):
            n = int(m.group(1))
            assert heads.get(n) == want, f"{phrase}{n} points at {heads.get(n)!r}, not {want!r}"
    # and no reference points at a section that does not exist
    for m in re.finditer(r"§(\d+)", GUIDE):
        assert int(m.group(1)) in heads, f"§{m.group(1)} is not a section"
