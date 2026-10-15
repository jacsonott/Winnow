"""The lateral-movement tab, mounted for real: enable the plugin, import
a logon table, open the tab, and confirm a shipped KAPE default binds to
the table and Build draws a graph. This is the class of bug a UI test
exists for — a plugin whose module throws on mount ships past every
backend test as a blank tab."""

from __future__ import annotations

import json
import urllib.request

import time

import pytest

pytestmark = pytest.mark.ui

# A real EvtxECmd export shape — the full header set, so the shipped
# defaults (tied to "Event logs (EvtxECmd)") bind to it.
_EVTX_COLS = ("RecordNumber,EventRecordId,TimeCreated,EventId,Level,Provider,Channel,ProcessId,"
              "ThreadId,Computer,ChunkNumber,UserId,MapDescription,UserName,RemoteHost,PayloadData1,"
              "PayloadData2,PayloadData3,PayloadData4,PayloadData5,PayloadData6,ExecutableInfo,"
              "HiddenRecord,SourceFile,Keywords,ExtraDataOffset,Payload")


def _wait_for_source(pg, name, timeout=25.0):
    """Wait for an imported table to appear. Polled from Python because
    page.wait_for_function does NOT await a promise-returning predicate:
    `() => loadSources().then(...)` hands it a Promise, which is truthy,
    so the wait passed instantly and the test raced the import."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        names = pg.evaluate(
            "() => __winnow.loadSources().then(() => __winnow.S.sources.map((s) => s.name))")
        if name in names:
            return
        time.sleep(0.25)
    raise AssertionError(f"{name} never appeared in S.sources")


def _evtx_row(rn, ts, eid, comp, user, remote, p2):
    vals = {"RecordNumber": rn, "TimeCreated": ts, "EventId": eid, "Channel": "Security",
            "Computer": comp, "UserName": user, "RemoteHost": remote, "PayloadData2": p2}
    return ",".join(str(vals.get(c, "")) for c in _EVTX_COLS.split(","))


LOGONS = _EVTX_COLS + "\n" + "\n".join([
    _evtx_row(1, "2026-03-14 08:00:00", 4624, "DC01", "alice", "10.0.0.9", "LogonType 3"),
    _evtx_row(2, "2026-03-14 09:00:00", 4624, "DC01", "bob", "10.0.0.9", "LogonType 3"),
    _evtx_row(3, "2026-03-15 10:00:00", 4624, "DC01", "alice", "10.0.0.5", "LogonType 10"),
]) + "\n"


def _post(server, route, body):
    req = urllib.request.Request(
        server.rstrip("/") + route, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def _call(server, route, method="GET"):
    req = urllib.request.Request(server.rstrip("/") + route, method=method,
                                 headers={"X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def test_lateral_movement_tab_mounts_binds_defaults_and_builds(browser, server, tmp_path):
    _post(server, "/api/plugins/toggle", {"fs_name": "lateral_movement", "scope": "on_all"})
    # The event types this test saves are machine-level plugin storage, so
    # they outlive the case; what was saved before goes back afterwards.
    saved = _call(server, "/api/plugin/lateral_movement/defs")["saved"]
    f = tmp_path / "logons.csv"
    f.write_text(LOGONS, encoding="utf-8")
    _post(server, "/api/ingest/jobs/path", {"path": str(f), "name": "logons.csv", "kind": "csv"})

    ctx = browser.new_context(viewport={"width": 1300, "height": 850})
    # pagesMenu: false for the same reason tests/ui/conftest.py does it —
    # this file clicks the plugin's own .tab-plugin node, which the Pages
    # dropdown (the default now) collapses into one button.
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.setItem('winnow.appearance',"
                        " JSON.stringify({ splash: false, pagesMenu: false }))")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_selector(".row")
        # Wait for the logon import to land, then activate the plugin tab.
        _wait_for_source(pg, "logons.csv")
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

        # A new movement type can be defined right from the panel. The
        # builder reads one table's columns and guesses each role from
        # them: UserName for the user, not the SID in UserId, which the
        # old union-of-every-table guess picked.
        before = pg.locator(".lm-events .lm-event").count()
        pg.locator("button", has_text="+ New event type").click()
        pg.wait_for_selector("#modal:not([hidden]) .lm-eb")
        role = lambda r: pg.locator(f"#modal select[data-role='{r}']").input_value()
        assert (role("src_col"), role("dst_col"), role("label_col"), role("time_col")) == (
            "RemoteHost", "Computer", "UserName", "TimeCreated")

        # The preview counts what the event finds in that table before it
        # is saved: all three logons, two links, three hosts.
        pg.wait_for_function(
            "() => [...document.querySelectorAll('#modal .lm-eb-stat b')].map((b) => b.textContent).join() === '3,2,3'",
            timeout=10_000)

        # A condition's value chips come from the column itself.
        pg.locator("#modal button", has_text="+ Add a condition").click()
        pg.locator("#modal .lm-eb-cond select[aria-label='Condition column']").select_option("UserName")
        pg.locator("#modal .lm-eb-chip", has_text="alice").click()
        assert pg.locator("#modal .lm-eb-cond input").input_value() == "alice"
        pg.wait_for_function(
            "() => document.querySelector('#modal .lm-eb-stat b').textContent === '2'", timeout=10_000)

        # A name already in the list is refused before it is saved: a
        # saved event sharing a shipped one's name ticked and unticked
        # together with it.
        name = pg.locator("#lmEbName")
        name.fill(labels[0])
        assert pg.locator("#modal .lm-eb-save").is_disabled()
        assert "already the name" in pg.locator("#modal .lm-eb-hint").inner_text()
        name.fill("My custom hop")
        pg.locator("#modal .lm-eb-save").click()
        pg.wait_for_function(
            "(n) => document.querySelectorAll('.lm-events .lm-event').length > n", arg=before)
        mine = pg.locator(".lm-events .lm-event", has_text="My custom hop")
        assert mine.locator("input").is_checked(), "a new event is ticked where it binds"

        # Events… edits it in the same builder, name and all, and the
        # tick follows the rename.
        pg.locator("button", has_text="Events…").click()
        pg.locator("#modal .lm-mgr-row", has_text="My custom hop").locator("button", has_text="Edit").click()
        pg.wait_for_selector("#modal:not([hidden]) .lm-eb")
        assert name.input_value() == "My custom hop"
        assert pg.locator("#modal .lm-eb-cond input").input_value() == "alice"
        name.fill("My renamed hop")
        pg.locator("#modal .lm-eb-save").click()
        pg.wait_for_selector("#modal:not([hidden]) .lm-mgr")
        assert "My renamed hop" in pg.locator("#modal .lm-mgr-name").all_inner_texts()
        pg.locator("#modal button", has_text="Close").click()
        assert pg.locator(".lm-events .lm-event", has_text="My renamed hop").locator("input").is_checked()
        assert not any("My custom hop" in t for t in pg.locator(".lm-event-name").all_inner_texts())
        assert not errors, errors
    finally:
        ctx.close()
        # The table and the saved event type both stayed behind for every
        # later module in the shard (#227).
        try:
            _post(server, "/api/plugin/lateral_movement/defs", {"saved": saved})
            for src in _call(server, "/api/sources"):
                if src["name"] == "logons.csv":
                    _call(server, f"/api/source/{src['id']}", "DELETE")
        finally:
            _post(server, "/api/plugins/toggle", {"fs_name": "lateral_movement", "scope": "off_all"})
