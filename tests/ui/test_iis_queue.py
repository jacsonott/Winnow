"""IIS logs in the frontend: recognised by name exactly as the server
recognises them (merge.js keeps a twin of iisread's rule), ahead of any
plugin that claims .log, and imported end to end through the queue."""
from __future__ import annotations

from pathlib import Path

import pytest

from winnow import iisread

pytestmark = pytest.mark.ui

NAMES = [
    "u_ex240501.log", "ex24050113.log", "u_ex2405.log", "u_extend1.log", "U_EX240501.LOG",
    "u_ex240501 (2).log", "nc240501.log", "ncsa1.log", "in240501.log", "inetsv3.log",
    r"C:\inetpub\logs\LogFiles\W3SVC2\u_ex240501.log", "/e/W3SVC1/u_ex240501.log",
    "hostd.log", "u_ex240501.log.1", "u_ex2405011.log", "ex241301.log", "in1234.log",
    "ex240532.log", "ex240500.log", "ex24050124.log", "my_ex240501.log", "extend.log",
]


def test_the_client_recognises_the_names_the_server_does(page):
    got = page.evaluate("(names) => names.map((n) => __winnow.iisLogFormat(n))", NAMES)
    assert got == [iisread.log_format(n) for n in NAMES]
    assert page.evaluate("() => __winnow.importKindFor('u_ex240501.log')") == "iis"
    assert page.evaluate("() => __winnow.importKindFor('hostd.log')") == "text"
    item = page.evaluate("() => __winnow.queueItem({ path: '/x/u_ex240501.log' }, 'u_ex240501.log')")
    assert item["kind"] == "iis" and item["configured"] is True
    # The folder-import chip is on in the defaults, and says what it is.
    assert "iis" in page.evaluate("() => __winnow.defaultImportExtensions()")
    assert page.evaluate("() => __winnow.importTypeLabel('iis')") == "IIS logs"


def test_a_plugin_claiming_log_does_not_take_an_iis_log(page):
    page.evaluate("() => { window.__savedFormats = __winnow.S.pluginFormats;"
                  " __winnow.S.pluginFormats = [{ id: 'esxi', label: 'ESXi', extensions: ['.log'] }]; }")
    try:
        assert page.evaluate("() => __winnow.pluginFormatFor('u_ex240501.log')") is None
        assert page.evaluate("() => __winnow.pluginFormatFor('hostd.log').id") == "esxi"
    finally:
        page.evaluate("() => { __winnow.S.pluginFormats = window.__savedFormats; }")


def test_an_iis_log_queues_and_imports_as_one(page, ui_csv):
    p = Path(ui_csv).parent / "u_ex240501.log"
    p.write_text("#Software: Microsoft Internet Information Services 10.0\r\n"
                 "#Fields: date time cs-method cs-uri-stem sc-status\r\n"
                 "2024-05-01 00:00:01 GET /a 200\r\n"
                 "#Fields: date time cs-method cs-uri-stem sc-status c-ip\r\n"
                 "2024-05-01 00:00:02 POST /b 500 203.0.113.9\r\n", newline="")
    try:
        page.evaluate("(p) => { __winnow.S.importQueue = []; __winnow.queuePaths([{ path: p, name: 'u_ex240501.log' }]); __winnow.openImportModal(); }", str(p))
        page.wait_for_selector("#modal:not([hidden])")
        row = page.locator("#modalBody .session-row", has_text="u_ex240501.log")
        assert "IIS log · W3C" in row.inner_text()
        assert row.locator("button", has_text="Preview").is_disabled()
        assert row.locator("button", has_text="As text").is_visible()
        page.locator("#modalBody button", has_text="Import all queued").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'u_ex240501.log' && s.row_count === 2)", timeout=20_000)
        cols = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'u_ex240501.log').columns.map((c) => c.name)")
        assert cols == ["Timestamp (UTC)", "date", "time", "cs-method", "cs-uri-stem", "sc-status", "c-ip"]
    finally:
        page.evaluate("""async () => {
          __winnow.S.importQueue = [];
          const ss = await __winnow.api('/api/sources');
          for (const s of ss.filter((s) => s.name === 'u_ex240501.log'))
            await __winnow.api('/api/source/' + s.id, { method: 'DELETE' });
          await __winnow.loadSources();
        }""")
