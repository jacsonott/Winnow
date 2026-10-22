"""IIS logs in the frontend: recognised by name exactly as the server
recognises them (merge.js keeps a twin of iisread's rule), ahead of any
plugin that claims .log, previewed and configured from the queue (the
format the name says or another, a type per column), and imported end to
end through it."""
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
    assert item["kind"] == "iis" and item["configured"] is False and item["log_format"] is None
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


LOG = ("#Software: Microsoft Internet Information Services 10.0\r\n"
       "#Fields: date time cs-method cs-uri-stem sc-status\r\n"
       "2024-05-01 00:00:01 GET /a 200\r\n"
       "#Fields: date time cs-method cs-uri-stem sc-status c-ip\r\n"
       "2024-05-01 00:00:02 POST /b 500 203.0.113.9\r\n")
W3C_COLUMNS = ["Timestamp (UTC)", "date", "time", "cs-method", "cs-uri-stem", "sc-status", "c-ip"]

DROP = """async (name) => {
  __winnow.S.importQueue = [];
  const ss = await __winnow.api('/api/sources');
  for (const s of ss.filter((s) => s.name === name))
    await __winnow.api('/api/source/' + s.id, { method: 'DELETE' });
  await __winnow.loadSources();
}"""


def _heads(page):
    return page.locator("#modalBody .preview-colname").all_inner_texts()


def _type_select(page, column):
    return page.locator("#modalBody .preview-tbl th",
                        has=page.locator(".preview-colname", has_text=column)).locator("select")


def test_an_iis_log_previews_configures_and_imports(page, ui_csv):
    """Preview & configure was greyed out on an IIS log: the analyst could
    neither see the table before importing it nor change a column's type."""
    p = Path(ui_csv).parent / "u_ex240501.log"
    p.write_text(LOG, newline="")
    try:
        page.evaluate("(p) => { __winnow.S.importQueue = []; __winnow.queuePaths([{ path: p, name: 'u_ex240501.log' }]); __winnow.openImportModal(); }", str(p))
        page.wait_for_selector("#modal:not([hidden])")
        row = page.locator("#modalBody .session-row", has_text="u_ex240501.log")
        assert "default settings · IIS log · W3C" in row.inner_text()
        assert row.locator("button", has_text="As text").is_visible()
        row.locator("button", has_text="Preview & configure").click()
        page.wait_for_selector("#modalBody .preview-tbl")
        assert page.locator("#modalBody select").first.input_value() == "iis:w3c"
        assert page.locator("#modalBody input[type=checkbox]").is_disabled()
        assert "IIS log, W3C · showing first 2 rows" in page.locator("#modalBody .note-status").first.inner_text()
        # Both headers' fields, the second's c-ip included.
        assert _heads(page) == W3C_COLUMNS
        assert _type_select(page, "sc-status").input_value() == "number"
        _type_select(page, "sc-status").select_option("text")
        page.locator("#modalBody button", has_text="Use these settings").click()

        row = page.locator("#modalBody .session-row", has_text="u_ex240501.log")
        assert "configured · IIS log · W3C" in row.inner_text()
        item = page.evaluate("() => __winnow.S.importQueue[0]")
        assert item["kind"] == "iis" and item["log_format"] == "w3c"
        assert item["column_types"]["sc-status"] == "text"
        # Opened again, it shows what was set rather than starting over.
        row.locator("button", has_text="Preview & configure").click()
        page.wait_for_selector("#modalBody .preview-tbl")
        assert _type_select(page, "sc-status").input_value() == "text"
        page.locator("#modalBody button", has_text="Use these settings").click()

        page.locator("#modalBody button", has_text="Import all queued").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'u_ex240501.log' && s.row_count === 2)", timeout=20_000)
        cols = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'u_ex240501.log').columns")
        assert [c["name"] for c in cols] == W3C_COLUMNS
        assert {c["name"]: c["type"] for c in cols}["sc-status"] == "text"
    finally:
        page.evaluate(DROP, "u_ex240501.log")


def test_an_uploaded_iis_log_previews_in_the_format_picked(page):
    """A browser-picked file previews by upload, and the format can be
    changed from the one its name says: this W3C log is named like an
    NCSA one."""
    try:
        page.evaluate("""async (text) => {
          __winnow.S.importQueue = [];
          await __winnow.queueFiles([new File([text], 'nc240501.log')]);
          __winnow.openImportModal();
        }""", LOG)
        page.wait_for_selector("#modal:not([hidden])")
        row = page.locator("#modalBody .session-row", has_text="nc240501.log")
        assert "IIS log · NCSA" in row.inner_text()
        row.locator("button", has_text="Preview & configure").click()
        page.wait_for_selector("#modalBody .preview-tbl")
        assert page.locator("#modalBody select").first.input_value() == "iis:ncsa"
        assert _heads(page)[1:8] == iisread.NCSA_COLUMNS
        page.locator("#modalBody select").first.select_option("iis:w3c")
        page.wait_for_function("(want) => JSON.stringify([...document.querySelectorAll('#modalBody .preview-colname')]"
                               ".map((n) => n.textContent)) === JSON.stringify(want)", arg=W3C_COLUMNS, timeout=10_000)
        page.locator("#modalBody button", has_text="Use these settings").click()
        row = page.locator("#modalBody .session-row", has_text="nc240501.log")
        assert "configured · IIS log · W3C" in row.inner_text()
        page.locator("#modalBody button", has_text="Import all queued").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'nc240501.log' && s.row_count === 2)", timeout=20_000)
        cols = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'nc240501.log').columns.map((c) => c.name)")
        assert cols == W3C_COLUMNS
    finally:
        page.evaluate(DROP, "nc240501.log")
