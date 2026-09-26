"""The plugins manager.

The panel this replaces showed a plugin that was switched OFF as its
folder name, a badge and the word "off" — nothing about what it was or
what it did, because a disabled plugin is never imported and the metadata
only existed after an import. The manager is built on reading that
metadata from the file instead (`plugin_api.static_meta`), so the first
thing these check is that a switched-off plugin actually describes itself.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _post(server, route, body):
    req = urllib.request.Request(
        server.rstrip("/") + route, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def _open(page):
    page.evaluate("() => __winnow.openPluginManager()")
    page.wait_for_selector("#modal:not([hidden])")
    page.wait_for_selector("#modalBody .pm-item", state="attached")


def _items(page):
    return page.evaluate("""() => [...document.querySelectorAll('#modalBody .pm-item')].map((i) => ({
      name: i.querySelector('.pm-item-label').textContent,
      tags: [...i.querySelectorAll('.pm-tag')].map((t) => t.textContent),
      summary: i.querySelector('.pm-item-sum').textContent,
    }))""")


def _detail(page):
    return page.evaluate("""() => {
      const d = document.querySelector('#modalBody .pm-detail');
      if (!d) return null;
      return {
        title: d.querySelector('h3').textContent,
        desc: (d.querySelector('.pm-desc') || {}).textContent || '',
        sections: [...d.querySelectorAll('.pm-sec-head')].map((h) => h.firstChild.textContent.trim()),
        kv: Object.fromEntries([...d.querySelectorAll('.pl-kv dt')].map(
          (dt) => [dt.textContent, dt.nextElementSibling.textContent])),
        foot: (d.querySelector('.pm-foot-note') || {}).textContent || '',
      };
    }""")


def _select(page, fs_or_name):
    page.evaluate("""(want) => {
      const i = [...document.querySelectorAll('#modalBody .pm-item')]
        .find((x) => x.querySelector('.pm-item-label').textContent.includes(want));
      if (i) i.click();
    }""", arg=fs_or_name)


@pytest.fixture(autouse=True)
def _close(page):
    yield
    page.keyboard.press("Escape")


def test_settings_keeps_one_line_and_a_button(page):
    """The whole point of moving out: Settings → Plugins is a summary, not
    a list of seven rows inside an accordion inside a shared dialog."""
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    page.click(".settings-section-head:has-text('Plugins')")
    page.wait_for_selector("#modalBody .settings-line", state="attached")
    line = page.evaluate("() => document.querySelector('#modalBody .settings-line-text').textContent")
    assert "installed" in line and " on of " in line, line
    assert page.evaluate(
        "() => !!document.querySelector('#modalBody .settings-line button')"), "no Manage button"


def test_a_switched_off_plugin_says_what_it_is(page):
    """The bug the whole change exists for. Every bundled example ships a
    name, a version and a description; none of it used to be reachable
    without switching the plugin on first."""
    _open(page)
    off = [i for i in _items(page) if "off" in i["tags"]]
    assert off, "no disabled plugins in the listing to check"
    for i in off:
        assert "_" not in i["name"], f"{i['name']} is still the folder name"
        assert i["summary"], f"{i['name']} has an empty summary"
        # ...and the tag says off exactly once. The old panel said it twice
        # — in the scope select and again in a right-hand column.
        assert i["summary"] != "off" and not i["summary"].startswith("off ")


def test_the_detail_pane_describes_a_plugin_that_is_not_running(page):
    _open(page)
    off = next(i for i in _items(page) if "off" in i["tags"])
    _select(page, off["name"])
    d = _detail(page)
    assert d["title"] == off["name"]
    assert len(d["desc"]) > 20, d["desc"]
    assert "What it would add" in d["sections"], d["sections"]
    assert d["kv"]["Status"] == "not imported"
    assert d["kv"]["Entry point"]
    assert "provides" in d["kv"]["Plugin API"]


def test_what_it_adds_comes_from_the_registry_once_it_is_running(page, server):
    """A running plugin has real registry data behind it; "what it would
    add" is only the answer for one that was deliberately not run, and the
    pane must switch between them."""
    listing = json.loads(urllib.request.urlopen(urllib.request.Request(
        server.rstrip("/") + "/api/plugins",
        headers={"X-Timeline-Lite-Client": "1"})).read())
    target = next((p for p in listing["plugins"] if not p["enabled"]), None)
    assert target, "every plugin is already on"
    fs = target["fs_name"]
    try:
        _open(page)
        _select(page, target["name"])
        assert "What it would add" in _detail(page)["sections"]

        _post(server, "/api/plugins/toggle", {"fs_name": fs, "scope": "on_all"})
        page.evaluate("() => __winnow.loadPlugins()")
        page.wait_for_function("(f) => (__winnow.S.plugins.find((p) => p.fs_name === f) || {}).enabled === true",
                               arg=fs)
        page.keyboard.press("Escape")
        _open(page)
        _select(page, target["name"])
        d = _detail(page)
        assert "What it adds" in d["sections"], d["sections"]
        assert "What it would add" not in d["sections"]
        assert d["kv"]["Status"] == "loaded — no errors"
    finally:
        _post(server, "/api/plugins/toggle", {"fs_name": fs, "scope": "off_all"})
        page.evaluate("() => __winnow.loadPlugins()")


def test_follow_drops_the_case_override(page, server):
    """The state one <select> could never spell: no override, follow the
    machine. Reachable here, and the foot sentence says which it is."""
    listing = json.loads(urllib.request.urlopen(urllib.request.Request(
        server.rstrip("/") + "/api/plugins",
        headers={"X-Timeline-Lite-Client": "1"})).read())
    target = listing["plugins"][0]
    fs, name = target["fs_name"], target["name"]
    try:
        _post(server, "/api/plugins/toggle", {"fs_name": fs, "scope": "on_case"})
        page.evaluate("() => __winnow.loadPlugins()")
        page.wait_for_function("(f) => (__winnow.S.plugins.find((p) => p.fs_name === f) || {}).case_override === true",
                               arg=fs)
        _open(page)
        _select(page, name)
        assert "only" in _detail(page)["foot"], _detail(page)["foot"]

        page.evaluate("""() => {
          const row = [...document.querySelectorAll('#modalBody .pl-scope-row')]
            .find((r) => r.querySelector('.pl-scope-label').textContent.startsWith('In '));
          [...row.querySelectorAll('button')].find((b) => b.textContent === 'Follow').click();
        }""")
        page.wait_for_function("(f) => (__winnow.S.plugins.find((p) => p.fs_name === f) || {}).case_override == null",
                               arg=fs)
        foot = _detail(page)["foot"]
        assert "every case on this machine" in foot, foot
    finally:
        page.keyboard.press("Escape")
        _post(server, "/api/plugins/toggle", {"fs_name": fs, "scope": "off_all"})
        page.evaluate("() => __winnow.loadPlugins()")


def test_the_plugin_folders_are_named_where_installing_puts_things(page):
    _open(page)
    dirs = page.evaluate("() => [...document.querySelectorAll('#modalBody .pl-dir')].map((d) => d.textContent)")
    assert dirs, "the manager does not say where plugins are read from"
    assert all(d.strip() for d in dirs)
