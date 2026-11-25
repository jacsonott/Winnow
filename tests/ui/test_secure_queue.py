"""Linux secure logs and auth.log in the frontend: recognized by name
exactly as the server recognizes them (merge.js keeps a twin of
secureread.name_says), below a plugin's claim (esxi_logs reads them for the
ESXi / UAC triage board), previewed from the queue with their fixed
columns and the year their first line will get, offered as a Read as
choice for other text and for a plugin-claimed one, and imported end to
end in the year the pane said."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from winnow import secureread

pytestmark = pytest.mark.ui

SECURE = "\n".join([
    "Dec 30 23:59:01 web01 sshd[1010]: Server listening on 0.0.0.0 port 22.",
    "Dec 31 08:01:02 web01 sshd[2211]: Accepted publickey for alice from 203.0.113.7 port 50122 ssh2",
    "Jan  2 03:14:15 web01 sshd[3001]: Failed password for invalid user oracle from 198.51.100.4 port 41234 ssh2",
    "Jan  2 03:20:00 web01 sudo:    alice : TTY=pts/0 ; PWD=/home/alice ; USER=root ; COMMAND=/bin/bash",
]) + "\n"
# The file's date: Jan 2 is in 2026, so Dec 30, a wrap before it, in 2025.
MODIFIED = datetime(2026, 1, 5, 12, tzinfo=timezone.utc).timestamp()

# One list through both sides: the server's rule and its merge.js twin
# must agree on every one, the ones that aren't secure logs included.
NAMES = [
    "secure", "secure-20240505", "secure.1", "secure.1.old", "Secure", "secure (2)", "secure-20240505 (2)",
    "auth.log", "auth.log.1", "auth.log-20240505", "auth (2).log", "auth.log (2).1",
    "/mnt/uac/[root]/var/log/secure", r"C:\cases\web01\var\log\auth.log.1",
    "secure.gz", "secure.txt", "secure.log", "auth", "auth.log.gz", "auth.log.txt", "my-secure",
    "insecure", "auth.logs", "messages", "syslog", "authlog",
]


def test_the_client_recognizes_what_the_server_does(page):
    named = page.evaluate("(names) => names.map((n) => __winnow.secureLogNamed(n))", NAMES)
    assert named == [secureread.name_says(n) for n in NAMES]
    assert any(named) and not all(named)
    # Gzipped, it is routed by the name inside: secure.gz is a secure log
    # (test_gzip_queue.py), and only a tarball is still an archive.
    kinds = page.evaluate("(names) => names.map((n) => __winnow.importKindFor(n))",
                          ["secure", "auth.log.1", "secure.gz", "secure.tar.gz", "messages", "secure.txt"])
    assert kinds == ["secure", "secure", "secure", "archive", "text", "csv"]
    item = page.evaluate("() => __winnow.queueItem({ path: '/x/secure' }, 'secure')")
    assert item["kind"] == "secure" and item["configured"] is True and item["year"] is None
    # The folder-import chip is on in the defaults, and says what it is.
    assert "secure" in page.evaluate("() => __winnow.defaultImportExtensions()")
    assert page.evaluate("() => __winnow.importTypeLabel('secure')") == "secure logs"


def test_a_plugins_claim_outranks_the_name(page):
    """esxi_logs claims secure* and every .log; turning it on has to keep
    giving it these, and the built-in reader is one Read as away."""
    item = page.evaluate("""() => {
      const was = __winnow.S.pluginFormats;
      __winnow.S.pluginFormats = [{ id: 'esxi_log', label: 'ESXi / Linux host log', extensions: ['.log'],
                                    filename_patterns: ['secure*', 'auth*'], options: [] }];
      try {
        const up = __winnow.queueItem({ file: new File(['x'], 'secure') }, 'secure');
        const byPath = __winnow.queueItem({ path: '/x/auth.log' }, 'auth.log');
        const other = __winnow.queueItem({ path: '/x/hostd.log' }, 'hostd.log');
        return { kinds: [up.kind, byPath.kind, other.kind],
                 up: __winnow.readAsOptions(up).map((o) => o[0]),
                 byPath: __winnow.readAsOptions(byPath).map((o) => o[0]),
                 other: __winnow.readAsOptions(other) };
      } finally { __winnow.S.pluginFormats = was; }
    }""")
    assert item["kinds"] == ["plugin", "plugin", "plugin"]
    assert item["up"] == ["plugin", "secure", "text"]
    assert item["byPath"] == ["plugin", "secure"]
    assert item["other"] is None


DROP = """async (names) => {
  __winnow.S.importQueue = [];
  const ss = await __winnow.api('/api/sources');
  for (const s of ss.filter((s) => names.includes(s.name)))
    await __winnow.api('/api/source/' + s.id, { method: 'DELETE' });
  await __winnow.loadSources();
}"""


def _hint(page, name):
    return page.locator("#modalBody .side-rail-item", has_text=name).locator(".side-rail-hint").inner_text()


def _heads_are(page, want):
    page.wait_for_function("(want) => JSON.stringify([...document.querySelectorAll('#modalBody .preview-colname')]"
                           ".map((n) => n.textContent)) === JSON.stringify(want)", arg=want, timeout=10_000)


def _first_cell_is(page, text):
    page.wait_for_function("(t) => document.querySelector('#modalBody .preview-tbl tr:nth-child(2) td')?.textContent === t",
                           arg=text, timeout=10_000)


def _year_hint(page):
    return page.locator("#modalBody .import-year-hint").inner_text()


def test_a_secure_log_by_path_previews_and_imports_in_the_year_shown(page, ui_csv):
    d = Path(ui_csv).parent / "var-log"
    d.mkdir(exist_ok=True)
    p = d / "secure"
    p.write_text(SECURE, newline="")
    os.utime(p, (MODIFIED, MODIFIED))
    try:
        page.evaluate("""async (path) => {
          __winnow.S.importQueue = [];
          await __winnow.queuePaths([{ path, name: 'secure' }]);
          __winnow.openImportModal();
        }""", str(p))
        page.wait_for_selector("#modal:not([hidden]) .import-pane .preview-tbl")
        assert page.evaluate("() => __winnow.S.importQueue.map((i) => i.kind)") == ["secure"]
        assert _hint(page, "secure") == "Secure log · a row per line"
        assert page.locator("#modalBody .import-readas button").all_inner_texts() == [
            "Secure log", "Delimited", "One line per row"]
        _heads_are(page, secureread.COLUMNS)
        _first_cell_is(page, "2025-12-30 23:59:01")
        page.wait_for_function("() => /blank: 2025/.test(document.querySelector('#modalBody .import-year-hint').textContent)")
        assert _year_hint(page) == "of the first line · blank: 2025, from the file’s modified date (2026-01-05)"
        foot = page.locator("#modalBody .import-preview .side-preview-foot").inner_text()
        assert foot == "Secure log · first 4 rows"
        assert page.locator("#modalBody .preview-tbl td", has_text="Failed password for invalid user oracle").count() == 1
        assert page.locator("#modalBody .preview-tbl td", has_text="SSH login failed").count() == 1

        # The year, set: the preview follows it, and so does the import.
        page.locator("#modalBody input.import-year").fill("2019")
        _first_cell_is(page, "2019-12-30 23:59:01")
        assert _year_hint(page) == "of the first line, as set"
        assert _hint(page, "secure") == "Secure log · first line in 2019"
        page.locator("#modalBody .import-foot .btn.primary").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'secure' && s.row_count === 4 && s.columns.length)", timeout=20_000)
        src = page.evaluate("() => __winnow.api('/api/sources').then((ss) => ss.find((s) => s.name === 'secure'))")
        assert [(c["name"], c["type"]) for c in src["columns"]] == list(zip(secureread.COLUMNS, secureread.COLUMN_TYPES))
        assert src["ingest"] == {"kind": "secure", "year": 2019}
    finally:
        page.evaluate(DROP, ["secure"])


def test_an_upload_is_dated_from_the_picked_files_date(page):
    """A browser-picked file is spooled when it imports, so the import
    window sends its lastModified: the preview and the import both date
    the first line from it, not from the upload."""
    try:
        page.evaluate("""async ([text, ms]) => {
          __winnow.S.importQueue = [];
          await __winnow.queueFiles([new File([text], 'auth.log.1', { lastModified: ms })]);
          __winnow.openImportModal();
        }""", [SECURE, MODIFIED * 1000])
        page.wait_for_selector("#modal:not([hidden]) .import-pane .preview-tbl")
        assert page.evaluate("() => __winnow.S.importQueue[0].kind") == "secure"
        _heads_are(page, secureread.COLUMNS)
        _first_cell_is(page, "2025-12-30 23:59:01")
        page.locator("#modalBody .import-foot .btn.primary").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'auth.log.1' && s.row_count === 4 && s.columns.length)", timeout=20_000)
        src = page.evaluate("() => __winnow.api('/api/sources').then((ss) => ss.find((s) => s.name === 'auth.log.1'))")
        assert src["ingest"] == {"kind": "secure", "year": 2025}
    finally:
        page.evaluate(DROP, ["auth.log.1"])


def test_other_text_can_be_read_as_a_secure_log(page):
    """messages, or a secure log saved under another name, is one choice
    away; a line that isn't a syslog line is kept whole in Message."""
    text = "-- Logs begin at Mon 2026-01-01 --\n" + SECURE
    try:
        page.evaluate("""async ([text, ms]) => {
          __winnow.S.importQueue = [];
          await __winnow.queueFiles([new File([text], 'web01-messages', { lastModified: ms })]);
          __winnow.openImportModal();
        }""", [text, MODIFIED * 1000])
        page.wait_for_selector("#modal:not([hidden]) .import-pane .preview-tbl")
        assert page.evaluate("() => __winnow.S.importQueue[0].kind") == "text"
        assert page.locator("#modalBody .import-readas button").all_inner_texts() == [
            "Delimited", "One line per row", "Key=value", "Body file", "Secure log"]
        page.locator("#modalBody .import-readas button[data-key=secure]").click()
        _heads_are(page, secureread.COLUMNS)
        assert _hint(page, "web01-messages") == "Secure log · a row per line"
        page.wait_for_function("() => /1 line isn’t a syslog line/.test("
                               "document.querySelector('#modalBody .import-preview .side-preview-foot').textContent)")
        page.locator("#modalBody .import-foot .btn.primary").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'web01-messages' && s.row_count === 5 && s.columns.length)", timeout=20_000)
    finally:
        page.evaluate(DROP, ["web01-messages"])
