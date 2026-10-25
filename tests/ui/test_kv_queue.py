"""Key=value logs in the frontend: recognised by their first line exactly
as the server recognises them (merge.js keeps a twin of kvread's rule),
asked of the server for a path and read from the head of an upload,
ahead of any plugin that claims .log, previewed and configured from the
queue (a type per column, by name), and imported end to end through it."""
from __future__ import annotations

from pathlib import Path

import pytest

from winnow import kvread

pytestmark = pytest.mark.ui

TRAFFIC = ("date=2026-10-23 time=05:39:00 devid=FG6H1ETB20902601 devname=Main-Firewall vd=root "
           "logid=0000000007 type=traffic subtype=forward level=notice srcip=192.168.1.50 srcport=54705 "
           'srcintf="internal" dstip=8.8.8.8 dstport=53 dstintf="wan1" sessionid=350696 action=accept '
           "policyid=10 service=DNS proto=6 duration=15 sentbyte=124 rcvdbyte=256")
EVENT = ('date=2026-10-23 time=05:40:12 devname="Main-Firewall" devid="FG6H1ETB20902601" '
         'logid="0100032001" type="event" subtype="system" level="information" vd="root" '
         'logdesc="Admin login successful" user="admin" msg="Administrator admin logged in"')
LOG = TRAFFIC + "\n" + EVENT + "\n"

TEXTS = [
    TRAFFIC, EVENT, "\n\n   " + TRAFFIC,
    '<189>date=2026-10-23 time=05:39:00 devname="FG" logid="0000000013"',
    "<189>Oct 23 05:39:00 10.0.0.1 date=2026-10-23 time=05:39:00 devname=FG x=1",
    'date=2026-10-23,time=05:39:00,devname="FG",logid="0000000013"',
    'date="2026-10-23" time="05:39:00" devname="FG"',
    "date=2026-10-23 time=05:39:00.123 log_id=0001001623 type=event",
    "", "\n\n", "date,time,srcip\n2026-10-23,05:39:00,1.2.3.4",
    "Oct 23 05:39:00 host sshd[42]: Accepted password for root from 10.0.0.1",
    "time=05:39:00 date=2026-10-23 devname=FG", "date=2026-10-23 time=05:39:00",
    "date=23/10/2026 time=05:39:00 devname=FG", "devname=FG date=2026-10-23 time=05:39:00 x=1",
    "some header line\n" + TRAFFIC,
]


def test_the_client_recognises_what_the_server_does(page):
    got = page.evaluate("(texts) => texts.map((t) => __winnow.looksLikeKvLog(t))", TEXTS)
    assert got == [kvread.looks_like(t) for t in TEXTS]
    assert any(got) and not all(got)
    # Only a name no reader claims is asked: raw text, or a .txt.
    names = ["fortigate.log", "export.txt", "fw", "fw.log.1", "forti.csv", "u_ex240501.log",
             "events.json", "mini.xlsx"]
    asked = page.evaluate("(names) => names.map((n) => __winnow.kvCandidate(n))", names)
    assert asked == [True, True, True, True, False, False, False, False]


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


def _hint(page, name):
    return page.locator("#modalBody .side-rail-item", has_text=name).locator(".side-rail-hint").inner_text()


def _pressed(page, seg):
    return page.locator(f"#modalBody .{seg} button[aria-pressed=true]").get_attribute("data-key")


def _heads_are(page, want):
    page.wait_for_function("(want) => JSON.stringify([...document.querySelectorAll('#modalBody .preview-colname')]"
                           ".map((n) => n.textContent)) === JSON.stringify(want)", arg=want, timeout=10_000)


def test_a_fortigate_log_by_path_previews_configures_and_imports(page, ui_csv):
    """A FortiGate log has no name of its own: the queue asks the server
    what a path holds, and shows it as a key=value log before it is
    imported rather than as one line per row."""
    d = Path(ui_csv).parent
    (d / "fortigate.log").write_text(LOG, newline="")
    (d / "notes.log").write_text("just words\n", newline="")
    try:
        page.evaluate("""async (d) => {
          __winnow.S.importQueue = [];
          await __winnow.queuePaths([{ path: d + '/fortigate.log', name: 'fortigate.log' },
                                     { path: d + '/notes.log', name: 'notes.log' }]);
        }""", str(d))
        assert page.evaluate("() => __winnow.S.importQueue.map((i) => i.kind)") == ["kv", "text"]
        page.evaluate("() => { __winnow.S.importQueue.pop(); __winnow.openImportModal(); }")
        page.wait_for_selector("#modal:not([hidden]) .import-pane .preview-tbl")
        assert _hint(page, "fortigate.log") == "Key=value log · a column per key"
        assert _pressed(page, "import-readas") == "kv"
        assert page.locator("#modalBody .import-readas button").all_inner_texts() == ["Key=value", "Delimited", "One line per row"]
        assert "Key=value log" in page.locator("#modalBody .import-preview .side-preview-foot").inner_text()
        # A column per key across both lines, in the order first seen; the
        # event line's keys after the traffic line's, quotes off.
        heads = _heads(page)
        assert heads[:4] == ["Timestamp (local)", "date", "time", "devid"]
        assert heads[-1] == "msg" and "srcintf" in heads and "logdesc" in heads
        assert page.locator("#modalBody .preview-tbl td", has_text="Administrator admin logged in").count() == 1
        assert _type_select(page, "srcport").input_value() == "number"
        _type_select(page, "srcport").select_option("text")
        item = page.evaluate("() => __winnow.S.importQueue[0]")
        assert item["kind"] == "kv" and item["column_types"]["srcport"] == "text"

        page.locator("#modalBody .import-foot .btn.primary").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'fortigate.log' && s.row_count === 2 && s.columns.length)", timeout=20_000)
        cols = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'fortigate.log').columns")
        assert [c["name"] for c in cols] == heads
        types = {c["name"]: c["type"] for c in cols}
        assert types["srcport"] == "text" and types["Timestamp (local)"] == "datetime"
    finally:
        page.evaluate(DROP, "fortigate.log")


def test_an_uploaded_fortigate_log_is_recognised_from_its_head(page):
    """A browser-picked file is recognised in the browser, from the same
    first bytes the server would read, and can be read one line per row
    and back."""
    try:
        page.evaluate("""async (text) => {
          __winnow.S.importQueue = [];
          await __winnow.queueFiles([new File([text], 'fw-export.txt'), new File(['a,b\\n1,2\\n'], 'other.txt')]);
        }""", LOG)
        assert page.evaluate("() => __winnow.S.importQueue.map((i) => i.kind)") == ["kv", "csv"]
        page.evaluate("() => { __winnow.S.importQueue.pop(); __winnow.openImportModal(); }")
        page.wait_for_selector("#modal:not([hidden]) .import-pane .preview-tbl")
        assert _heads(page)[:2] == ["Timestamp (local)", "date"]
        page.locator("#modalBody .import-readas button[data-key=text]").click()
        _heads_are(page, ["Message"])
        assert _hint(page, "fw-export.txt") == "Text · one line per row"
        page.locator("#modalBody .import-readas button[data-key=kv]").click()
        page.wait_for_function("() => document.querySelector('#modalBody .preview-colname')?.textContent === 'Timestamp (local)'", timeout=10_000)
        page.locator("#modalBody .import-foot .btn.primary").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'fw-export.txt' && s.row_count === 2 && s.columns.length)", timeout=20_000)
    finally:
        page.evaluate(DROP, "fw-export.txt")


def test_a_plugin_claiming_log_does_not_take_a_fortigate_log(page):
    page.evaluate("() => { window.__savedFormats = __winnow.S.pluginFormats;"
                  " __winnow.S.pluginFormats = [{ id: 'esxi', label: 'ESXi', extensions: ['.log'] }]; }")
    try:
        kinds = page.evaluate("""async (text) => {
          __winnow.S.importQueue = [];
          await __winnow.queueFiles([new File([text], 'fortigate.log'), new File(['hostd started\\n'], 'hostd.log')]);
          return __winnow.S.importQueue.map((i) => i.kind);
        }""", LOG)
        assert kinds == ["kv", "plugin"]
    finally:
        page.evaluate("() => { __winnow.S.pluginFormats = window.__savedFormats; __winnow.S.importQueue = []; }")
