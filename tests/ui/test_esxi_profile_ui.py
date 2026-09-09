"""The shipped ESXi / UAC triage profile is listed read-only alongside
KAPE, and applying it (with the esxi_logs plugin enabled and logs
imported) builds its overview dashboard, whose {{all:...}} widgets resolve
across every imported log."""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _post(server, route, body):
    req = urllib.request.Request(
        server.rstrip("/") + route, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def test_esxi_profile_lists_applies_and_builds(browser, server, tmp_path):
    _post(server, "/api/plugins/toggle", {"fs_name": "esxi_logs", "scope": "on_all"})
    # Import two real ESXi log shapes through the plugin format.
    (tmp_path / "hostd.log").write_text(
        "2024-01-05T13:22:01.1Z info hostd[1] [Originator@6876 sub=x] PowerOn VM web01\n"
        "2024-01-05T13:22:02.2Z info hostd[1] [Originator@6876 sub=x] Snapshot created web01\n")
    (tmp_path / "auth.log").write_text(
        "Jan  5 13:22:01 esxi01 sshd[2]: Accepted password for root from 10.0.0.5 port 22 ssh2\n"
        "Jan  5 13:22:05 esxi01 sshd[3]: Failed password for root from 10.0.0.9 port 41 ssh2\n")
    for f in ("hostd.log", "auth.log"):
        _post(server, "/api/ingest/plugin/path",
              {"path": str(tmp_path / f), "format_id": "esxi-logs.esxi_log", "options": {}})

    ctx = browser.new_context(viewport={"width": 1400, "height": 950})
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.setItem('winnow.appearance', JSON.stringify({ splash: false }))")
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_selector(".row")
        pg.evaluate("() => __winnow.loadSources()")

        listed = {b["name"]: b for b in json.loads(
            urllib.request.urlopen(server.rstrip('/') + "/api/plugin_bundles").read())}
        assert "ESXi / UAC triage" in listed
        prof = listed["ESXi / UAC triage"]
        assert prof["id"] < 0 and prof["shipped"] is True

        expected = len(prof["dashboard"])
        _post(server, f"/api/plugin_bundles/{prof['id']}/apply", {})
        pg.evaluate("async () => { await __winnow.loadDashboards(); }")
        boards = pg.evaluate("() => __winnow.S.dashboards.map((d) => d.name)")
        assert "ESXi / UAC triage" in boards
        did = pg.evaluate("() => __winnow.S.dashboards.find((d) => d.name === 'ESXi / UAC triage').id")
        pg.evaluate("(id) => __winnow.showDashboard(id)", did)
        pg.wait_for_selector("#dashboardview:not([hidden])")
        pg.wait_for_function(
            "(n) => document.querySelectorAll('#dashGrid .dash-card:not(.dash-add)').length === n",
            arg=expected, timeout=10_000)
        # the union widget totalled across BOTH logs (4 lines), proving
        # {{all:...}} spanned them rather than binding to one
        pg.wait_for_function(
            """() => { const c=[...document.querySelectorAll('#dashGrid .dash-card')].find(x=>/Log lines/.test(x.textContent));
                       return c && c.querySelector('.dash-stat') && c.querySelector('.dash-stat').textContent.replace(/,/g,'')==='4'; }""",
            timeout=10_000)
        # Leave the shared session case as found: drop the created
        # dashboard and the two imported log sources, and disable the
        # plugin again (see the shared-server pitfall — every later UI
        # module sees this case).
        pg.evaluate("""async () => {
          const h = { 'X-Timeline-Lite-Client': '1' };
          for (const d of await fetch('/api/dashboards', { headers: h }).then(r => r.json()))
            await fetch('/api/dashboards/' + d.id, { method: 'DELETE', headers: h });
          for (const s of __winnow.S.sources.filter(x => /\\.log$/.test(x.name)))
            await fetch('/api/source/' + s.id, { method: 'DELETE', headers: h });
        }""")
        _post(server, "/api/plugins/toggle", {"fs_name": "esxi_logs", "scope": "off_all"})
        pg.evaluate("async () => { __winnow.S.sourceId = null; __winnow.S.viewCache.clear();"
                    " await __winnow.loadSources(); await __winnow.loadPlugins(); }")
    finally:
        ctx.close()
        assert not errors, "uncaught JS errors: " + " | ".join(errors)
