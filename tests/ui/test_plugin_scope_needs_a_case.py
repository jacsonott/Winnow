"""Per-case plugin scopes need a case on screen.

Settings → Plugins offers four scopes per plugin: on/off for every case on
this machine, and on/off for this case only. The last two were gated on
`case_open`, which asks whether the SERVER still holds a Store — and going
back to the home screen only hides `#app` (`showHome`), so it stays true.

Opening Settings from the home screen therefore offered "this case only"
for a case nothing on that screen names, and choosing it wrote a
`plugin_overrides` entry into whichever case happened to still be open.
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


def _open_plugins_panel(page):
    # Called directly rather than via the ? hotkey: this file opens Settings
    # from the home screen too, where the grid does not have focus.
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    page.click(".settings-section-head:has-text('Plugins')")
    # attached, not visible: Settings' other sections are collapsed and
    # their selects resolve first.
    page.wait_for_selector("#modalBody .settings-section-body:not([hidden]) select", state="attached")


def _scopes(page):
    return page.evaluate("""() => {
      const s = [...document.querySelectorAll('#modalBody select')]
        .find((x) => x.nextElementSibling && x.nextElementSibling.classList.contains('session-name'));
      return s ? [...s.options].map((o) => ({ value: o.value, label: o.textContent, disabled: o.disabled })) : null;
    }""")


@pytest.fixture(autouse=True)
def _back_to_the_case(page):
    yield
    page.keyboard.press("Escape")
    page.evaluate("() => { document.getElementById('home').hidden = true; document.getElementById('app').hidden = false; }")


def test_a_case_on_screen_offers_all_four(page):
    _open_plugins_panel(page)
    opts = _scopes(page)
    assert opts is not None
    assert [o["value"] for o in opts] == ["on_all", "off_all", "on_case", "off_case"]


def test_the_case_scopes_name_the_case(page):
    """"this case only" beside nothing that says which case is the
    complaint; the brand button already answers it, so they use that."""
    label = page.evaluate("() => document.getElementById('brandLabel').textContent.trim()")
    _open_plugins_panel(page)
    per_case = [o["label"] for o in _scopes(page) if o["value"].endswith("_case")]
    assert per_case and all(label in o for o in per_case), (label, per_case)


def test_the_home_screen_offers_only_the_machine_wide_scopes(page):
    """The server still holds the case — that is the point. What changed is
    that nothing on screen names it."""
    page.evaluate("() => __winnow.showHome()")
    # Both halves, and settled: boot() reveals #app after its own fetch, so
    # asserting on #home alone can read a moment before that lands and the
    # panel then builds with a case on screen after all.
    page.wait_for_function(
        "() => document.getElementById('app').hidden && !document.getElementById('home').hidden")
    assert page.evaluate("() => __winnow.S.pluginsCaseOpen") is True, "the Store is still open, as it was"
    _open_plugins_panel(page)
    assert page.evaluate("() => document.getElementById('app').hidden") is True
    opts = _scopes(page)
    # Nothing CHOOSABLE beyond the two machine-wide scopes. A plugin the
    # still-open case has overridden also carries one disabled option
    # stating that, which is the point of it — see the test below.
    assert [o["value"] for o in opts if not o["disabled"]] == ["on_all", "off_all"], opts
    assert all(o["disabled"] for o in opts if o["value"].endswith("_case")), opts


def test_a_case_override_is_still_told_the_truth_from_home(page, server):
    """A plugin a case has turned off must not read as "On — all cases"
    just because its scopes are not on offer."""
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
        _open_plugins_panel(page)
        opts = _scopes(page)
        chosen = page.evaluate("""() => {
          const s = [...document.querySelectorAll('#modalBody select')]
            .find((x) => x.nextElementSibling && x.nextElementSibling.classList.contains('session-name'));
          return s ? s.options[s.selectedIndex].textContent : null; }""")
        assert "set by the open case" in chosen, (chosen, opts)
        assert any(o["disabled"] for o in opts), opts
    finally:
        page.keyboard.press("Escape")
        _post(server, "/api/plugins/toggle", {"fs_name": fs, "scope": "off_all"})
        page.evaluate("() => __winnow.loadPlugins()")

