"""The lateral-movement tab, mounted for real: enable the plugin, import
a logon table, open the tab, and confirm a shipped KAPE default binds to
the table and Build draws a graph. This is the class of bug a UI test
exists for — a plugin whose module throws on mount ships past every
backend test as a blank tab."""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui

LOGONS = ("SourceHost,DestHost,User,EventId,Channel,RemoteHost,Computer,UserName,TimeCreated,PayloadData2\n"
          "10.0.0.9,DC01,alice,4624,Security,10.0.0.9,DC01,alice,2026-03-14 08:00:00,LogonType 3\n"
          "10.0.0.9,DC01,bob,4624,Security,10.0.0.9,DC01,bob,2026-03-14 09:00:00,LogonType 3\n"
          "10.0.0.5,DC01,alice,4624,Security,10.0.0.5,DC01,alice,2026-03-15 10:00:00,LogonType 10\n")


def _post(server, route, body):
    req = urllib.request.Request(
        server.rstrip("/") + route, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def test_lateral_movement_tab_mounts_binds_defaults_and_builds(browser, server, tmp_path):
    _post(server, "/api/plugins/toggle", {"fs_name": "lateral_movement", "scope": "on_all"})
    f = tmp_path / "logons.csv"
    f.write_text(LOGONS, encoding="utf-8")
    _post(server, "/api/ingest/jobs/path", {"path": str(f), "name": "logons.csv", "kind": "csv"})

    ctx = browser.new_context(viewport={"width": 1300, "height": 850})
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.setItem('winnow.appearance', JSON.stringify({ splash: false }))")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_selector(".row")
        # Wait for the logon import to land, then activate the plugin tab.
        pg.wait_for_function(
            "() => __winnow.loadSources().then(() => __winnow.S.sources.some((s) => s.name === 'logons.csv'))",
            timeout=15_000)
        # The toggle happened server-side out of band; a real Settings
        # toggle calls this, so the test does too.
        pg.evaluate("() => __winnow.loadPlugins()")
        pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('lateral'))",
                             timeout=10_000)
        pg.locator(".tab-plugin", has_text="Lateral movement").click()

        # A shipped default binds to the logon table → its checkbox appears.
        pg.wait_for_selector(".lm-event", timeout=10_000)
        labels = pg.locator(".lm-event-name").all_inner_texts()
        assert any("4624" in l for l in labels), labels

        # Tick the first bound event and build.
        pg.locator(".lm-events .lm-event input[type=checkbox]").first.check()
        pg.locator("button", has_text="Build graph").click()
        pg.wait_for_function(
            "() => document.querySelector('.lm-legend') && document.querySelector('.lm-legend').style.display !== 'none'",
            timeout=10_000)
        assert "host" in pg.locator(".lm-bar .note-status").inner_text().lower()

        # The event panel collapses (it can be tall), and the header keeps
        # a selection summary so state is still visible when hidden.
        assert not pg.evaluate("() => document.querySelector('.lm-events').hidden")
        pg.locator(".lm-caret").click()
        pg.wait_for_function("() => document.querySelector('.lm-events').hidden")
        assert "selected" in pg.locator(".lm-panel-summary").inner_text()
        pg.locator(".lm-caret").click()
        pg.wait_for_function("() => !document.querySelector('.lm-events').hidden")

        # A new movement type can be defined right from the panel — the
        # editor pre-guesses columns, so this only needs a name and Save.
        before = pg.locator(".lm-events .lm-event").count()
        pg.locator("button", has_text="+ New event type").click()
        pg.wait_for_selector("#modal:not([hidden])")
        pg.locator("#modal .confirm-input").first.fill("My custom hop")
        # source/dest are pre-guessed from the logon table's column names.
        assert pg.locator("#modal select").first.input_value() != "", "source column should be pre-guessed"
        pg.locator("#modal button", has_text="Save event").click()
        pg.wait_for_function(
            "(n) => document.querySelectorAll('.lm-events .lm-event').length > n", arg=before)
        assert any("My custom hop" in t for t in pg.locator(".lm-event-name").all_inner_texts())
        assert not errors, errors
    finally:
        ctx.close()
        _post(server, "/api/plugins/toggle", {"fs_name": "lateral_movement", "scope": "off_all"})
