"""The lateral-movement tab, mounted for real: enable the plugin, import
a logon table, open the tab, and confirm the shipped KAPE defaults bind
to the table and the views draw what they record. This is the class of
bug a UI test exists for — a plugin whose module throws on mount ships
past every backend test as a blank tab."""

from __future__ import annotations

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

# The tab mounts after the click that opens it: until then there is no status to read.
STATUS = "(document.querySelector('.lm-status')?.textContent || '')"


def _status_has(pg, text):
    pg.wait_for_function(f"(t) => {STATUS}.includes(t)", arg=text, timeout=15_000)


def _right_click_host(pg, name):
    """A host on the canvas, by where the view says it drew it
    (canvas.lmHosts: name -> {x, y} for every host label on screen)."""
    pg.wait_for_function(
        "(n) => { const c = document.querySelector('.lm-stage canvas'); return !!(c && c.lmHosts && c.lmHosts[n]); }",
        arg=name, timeout=10_000)
    at = pg.evaluate("(n) => document.querySelector('.lm-stage canvas').lmHosts[n]", name)
    box = pg.locator(".lm-stage canvas").bounding_box()
    pg.mouse.click(box["x"] + at["x"], box["y"] + at["y"], button="right")


def test_lateral_movement_tab_draws_binds_defaults_and_defines_events(own_page, tmp_path, server_post,
                                                                       server_call):
    server_post("/api/plugins/toggle", {"fs_name": "lateral_movement", "scope": "on_all"})
    # The event types this test saves are machine-level plugin storage, so
    # they outlive the case; what was saved before goes back afterwards.
    saved = server_call("/api/plugin/lateral_movement/defs")["saved"]
    f = tmp_path / "logons.csv"
    f.write_text(LOGONS, encoding="utf-8")
    server_post("/api/ingest/jobs/path", {"path": str(f), "name": "logons.csv", "kind": "csv"})

    try:
        with own_page(viewport=(1300, 850)) as pg:
            _wait_for_source(pg, "logons.csv")
            # The toggle happened server-side out of band; a real Settings
            # toggle calls this, so the test does too.
            pg.evaluate("() => __winnow.loadPlugins()")
            pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('lateral'))",
                                 timeout=10_000)
            pg.locator(".tab-plugin", has_text="Lateral movement").click()

            # Opened on a case for the first time, it ticks every shipped
            # event that binds to a table and draws them, rather than
            # waiting on an empty panel. Three logons: two sources into
            # DC01, three (hosts, account) links.
            _status_has(pg, "3 hosts · 3 links · 3 rows")
            assert pg.locator(".lm-seg button[data-mode=chain]").get_attribute("aria-selected") == "true"

            # The event panel starts closed once something is on; its head
            # says what is.
            assert pg.evaluate("() => document.querySelector('.lm-events').hidden")
            assert "6 of 6 on" in pg.locator(".lm-panel-summary").inner_text()
            pg.locator(".lm-caret").click()
            pg.wait_for_function("() => !document.querySelector('.lm-events').hidden")
            logon = pg.locator(".lm-events .lm-event", has_text="4624")
            assert "3 rows" in logon.inner_text()

            # Unticking an event reads the feed again without it.
            logon.locator("input").uncheck()
            pg.wait_for_function("() => (document.querySelector('.lm-empty') || {}).textContent?.includes('draw no hops')",
                                 timeout=15_000)
            assert "5 of 6 on" in pg.locator(".lm-panel-summary").inner_text()
            logon.locator("input").check()
            _status_has(pg, "3 links")

            # By account lists each account the hops carry.
            pg.locator(".lm-seg button[data-mode=accounts]").click()
            pg.wait_for_selector(".lm-acct tbody tr")
            accounts = pg.evaluate("() => [...document.querySelectorAll('.lm-acct tbody tr')].map((r) => r.dataset.account)")
            assert sorted(accounts) == ["ALICE", "BOB"]

            # A host's right-click is the way into a pivot: where its
            # access came from, and where it went next.
            pg.locator(".lm-seg button[data-mode=matrix]").click()
            _right_click_host(pg, "DC01")
            pg.locator(".menu", has_text="Pivot from DC01").locator("text=Pivot from DC01").click()
            _status_has(pg, "3 hosts within three hops of DC01")
            assert "Pivot: DC01" in pg.locator(".lm-seg button[data-mode=pivot]").inner_text()

            # A new movement type can be defined right from the panel. The
            # builder reads one table's columns and guesses each role from
            # them: UserName for the user, not the SID in UserId, which the
            # old union-of-every-table guess picked. The acting account is
            # never guessed: a wrong one would invent credential switches.
            before = pg.locator(".lm-events .lm-event").count()
            pg.locator("button", has_text="+ New event type").click()
            pg.wait_for_selector("#modal:not([hidden]) .lm-eb")
            role = lambda r: pg.locator(f"#modal select[data-role='{r}']").input_value()
            assert (role("src_col"), role("dst_col"), role("label_col"), role("actor_col"), role("time_col")) == (
                "RemoteHost", "Computer", "UserName", "", "TimeCreated")

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
            name.fill(pg.locator(".lm-event-name").first.inner_text())
            assert pg.locator("#modal .lm-eb-save").is_disabled()
            assert "already the name" in pg.locator("#modal .lm-eb-hint").inner_text()
            name.fill("My custom hop")
            pg.locator("#modal .lm-eb-save").click()
            pg.wait_for_function(
                "(n) => document.querySelectorAll('.lm-events .lm-event').length > n", arg=before)
            mine = pg.locator(".lm-events .lm-event", has_text="My custom hop")
            assert mine.locator("input").is_checked(), "a new event is ticked where it binds"
            # and the panel says what it added once the feed has it
            pg.wait_for_function(
                "() => document.querySelector('.lm-panel-summary').textContent.includes('Added \"My custom hop\": 2 rows')",
                timeout=15_000)

            # Events… edits it in the same builder, name and all, and the
            # tick follows the rename. It opens on Yours, where a name is
            # the way into the builder.
            pg.locator("button", has_text="Events…").click()
            pg.locator("#modal .lm-mgr-row", has_text="My custom hop").locator(".lm-mgr-name").click()
            pg.wait_for_selector("#modal:not([hidden]) .lm-eb")
            assert name.input_value() == "My custom hop"
            assert pg.locator("#modal .lm-eb-cond input").input_value() == "alice"
            name.fill("My renamed hop")
            pg.locator("#modal .lm-eb-save").click()
            pg.wait_for_selector("#modal:not([hidden]) .lm-mgr-panes")
            assert "My renamed hop" in pg.locator("#modal .lm-mgr-name").all_inner_texts()
            pg.locator("#modal button", has_text="Close").click()
            assert pg.locator(".lm-events .lm-event", has_text="My renamed hop").locator("input").is_checked()
            assert not any("My custom hop" in t for t in pg.locator(".lm-event-name").all_inner_texts())
    finally:
        # The table and the saved event type both stayed behind for every
        # later module in the shard (#227).
        try:
            server_post("/api/plugin/lateral_movement/defs", {"saved": saved})
            for src in server_call("/api/sources"):
                if src["name"] == "logons.csv":
                    server_call(f"/api/source/{src['id']}", "DELETE")
        finally:
            server_post("/api/plugins/toggle", {"fs_name": "lateral_movement", "scope": "off_all"})
