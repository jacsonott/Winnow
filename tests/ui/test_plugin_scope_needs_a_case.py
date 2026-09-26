"""Per-case plugin scopes need a case on screen.

The plugins manager offers scope as two controls: a machine-wide default
("Everywhere"), and — when a case is on screen — an override for that case
that can Follow the default, or override it on or off.

The per-case half was once gated on `case_open`, which asks whether the
SERVER still holds a Store — and going back to the home screen only hides
`#app` (`showHome`), so it stays true. Opening the manager from the home
screen therefore offered "this case only" for a case nothing on that
screen names, and choosing it wrote a `plugin_overrides` entry into
whichever case happened to still be open.
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


def _open_manager(page):
    # Called directly rather than through Settings: this file opens the
    # manager from the home screen too, where the grid has no focus.
    page.evaluate("() => __winnow.openPluginManager()")
    page.wait_for_selector("#modal:not([hidden])")
    page.wait_for_selector("#modalBody .pl-scope-row", state="attached")


def _scope_rows(page):
    return page.evaluate("""() => [...document.querySelectorAll('#modalBody .pl-scope-row')].map((r) => ({
      label: r.querySelector('.pl-scope-label').textContent,
      buttons: [...r.querySelectorAll('button')].map((b) => ({
        label: b.textContent, on: b.getAttribute('aria-pressed') === 'true', disabled: b.disabled })),
    }))""")


@pytest.fixture(autouse=True)
def _back_to_the_case(page):
    yield
    page.keyboard.press("Escape")
    page.evaluate("() => { document.getElementById('home').hidden = true; document.getElementById('app').hidden = false; }")


def test_a_case_on_screen_offers_both_halves(page):
    _open_manager(page)
    rows = _scope_rows(page)
    assert [r["label"] for r in rows][:1] == ["Everywhere"]
    assert len(rows) == 2, rows
    assert [b["label"] for b in rows[0]["buttons"]] == ["Off", "On"]
    assert [b["label"] for b in rows[1]["buttons"]] == ["Follow", "On", "Off"]


def test_the_case_scope_names_the_case(page):
    """A per-case control beside nothing that says which case is the
    complaint; the brand button already answers it, so it uses that."""
    label = page.evaluate("() => document.getElementById('brandLabel').textContent.trim()")
    _open_manager(page)
    rows = _scope_rows(page)
    assert label in rows[1]["label"], (label, rows[1]["label"])


def test_the_home_screen_offers_only_the_machine_wide_scope(page):
    """The server still holds the case — that is the point. What changed is
    that nothing on screen names it."""
    page.evaluate("() => __winnow.showHome()")
    # Both halves, and settled: boot() reveals #app after its own fetch, so
    # asserting on #home alone can read a moment before that lands and the
    # manager then builds with a case on screen after all.
    page.wait_for_function(
        "() => document.getElementById('app').hidden && !document.getElementById('home').hidden")
    assert page.evaluate("() => __winnow.S.pluginsCaseOpen") is True, "the Store is still open, as it was"
    _open_manager(page)
    assert page.evaluate("() => document.getElementById('app').hidden") is True
    rows = _scope_rows(page)
    assert [r["label"] for r in rows] == ["Everywhere"], rows


def test_a_case_override_is_still_told_the_truth_from_home(page, server):
    """A plugin a case has turned on must not read as plain machine state
    just because its per-case control is not on offer."""
    plugins = json.loads(urllib.request.urlopen(urllib.request.Request(
        server.rstrip("/") + "/api/plugins",
        headers={"X-Timeline-Lite-Client": "1"})).read())["plugins"]
    fs = plugins[0]["fs_name"]
    _post(server, "/api/plugins/toggle", {"fs_name": fs, "scope": "on_case"})
    try:
        page.evaluate("() => __winnow.loadPlugins()")
        page.wait_for_function("(f) => (__winnow.S.plugins.find((p) => p.fs_name === f) || {}).case_override === true",
                               arg=fs)
        page.evaluate("() => __winnow.showHome()")
        page.wait_for_function(
            "() => document.getElementById('app').hidden && !document.getElementById('home').hidden")
        # plugins[0] is also the manager's default selection, so the
        # detail pane is already showing the overridden plugin.
        _open_manager(page)
        note = page.evaluate("() => (document.querySelector('#modalBody .pl-scope-note') || {}).textContent || ''")
        assert "open but not on screen" in note, note
        assert _scope_rows(page)[0]["label"] == "Everywhere"
    finally:
        page.keyboard.press("Escape")
        _post(server, "/api/plugins/toggle", {"fs_name": fs, "scope": "off_all"})
        page.evaluate("() => __winnow.loadPlugins()")
