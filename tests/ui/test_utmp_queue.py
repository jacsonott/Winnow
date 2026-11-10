"""Linux login records in the frontend: recognized by name exactly as the
server recognizes them (merge.js keeps a twin of utmpread.log_kind), and
renamed ones by their bytes (a twin of utmpread.looks_like) instead of
being skipped as binary; ahead of any plugin's claim; previewed with the
layout the file's bytes say; imported end to end from the queue."""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from winnow import utmpread

from test_utmp_logs import LAYOUT_KEYS, ROWS, SESSION, build

pytestmark = pytest.mark.ui

NAMES = [
    "wtmp", "btmp", "utmp", "WTMP", "wtmp.1", "btmp-20240501", "btmp-20240501.1", "wtmp.1.old",
    "wtmp (2)", "wtmp (2).1", "/mnt/img/var/log/wtmp", r"C:\uac\[root]\var\log\btmp.1",
    "wtmp.db", "lastlog", "wtmpx", "wtmp.txt", "my_wtmp", "wtmp.1.gz", "btmp.log", "wtmp-",
]

DROP = """async (name) => {
  __winnow.closeModal();
  __winnow.S.importQueue = [];
  const ss = await __winnow.api('/api/sources');
  for (const s of ss.filter((s) => s.name === name))
    await __winnow.api('/api/source/' + s.id, { method: 'DELETE' });
  await __winnow.loadSources();
}"""

# In-page: a File from base64 bytes, as a browser pick or drop would give.
QUEUE_UPLOAD = """async ([b64, name]) => {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  __winnow.S.importQueue = [];
  await __winnow.queueFiles([new File([bytes], name)]);
  return __winnow.S.importQueue.map((i) => i.kind);
}"""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _hint(page, name):
    return page.locator("#modalBody .side-rail-item", has_text=name).locator(".side-rail-hint").inner_text()


def test_the_client_recognizes_the_names_the_server_does(page):
    got = page.evaluate("(names) => names.map((n) => __winnow.utmpFileKind(n))", NAMES)
    assert got == [utmpread.log_kind(n) for n in NAMES]
    assert page.evaluate("() => __winnow.importKindFor('wtmp.1')") == "utmp"
    assert page.evaluate("() => __winnow.importKindFor('wtmp.db')") == "sqlite"
    item = page.evaluate("() => __winnow.queueItem({ path: '/x/btmp' }, 'btmp')")
    assert item["kind"] == "utmp" and item["configured"] is True
    # The folder-import chip is on in the defaults, and says what it is.
    assert "utmp" in page.evaluate("() => __winnow.defaultImportExtensions()")
    assert page.evaluate("() => __winnow.importTypeLabel('utmp')") == "wtmp/btmp/utmp"


def test_the_client_recognizes_renamed_records_as_the_server_does(page):
    blobs = [build(k, SESSION[:-1]) for k in LAYOUT_KEYS]
    blobs += [
        build("384-le"),                                        # an all-zero EMPTY slot says nothing
        build("384-le", SESSION[:-1]) + b"\0" * 7,              # not a whole number of records
        build("384-le", SESSION[:-1] + [dict(typ=0, user=b"old", sec=1)]),   # EMPTY with leftovers
        bytes(384 * 4),                                         # zeros
        b"\x89PNG\r\n\x1a\n" + bytes(760),                      # some other binary
        build("400-le", [dict(SESSION[3], sec=1_714_557_598 + i) for i in range(40)]),  # past the 8 KB probe
    ]
    got = page.evaluate("""(blobs) => blobs.map((b64) => {
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      return __winnow.looksLikeUtmp(bytes.subarray(0, 8192), bytes.length);
    })""", [_b64(b) for b in blobs])
    assert got == [utmpread.looks_like(b[:utmpread.PROBE_BYTES], len(b)) for b in blobs]
    assert got[:4] == [True] * 4 and got[4] is True and got[5:9] == [False] * 4


def test_a_plugin_claiming_the_name_does_not_take_login_records(page):
    page.evaluate("() => { window.__savedFormats = __winnow.S.pluginFormats;"
                  " __winnow.S.pluginFormats = [{ id: 'linux', label: 'Linux', filename_patterns: ['*tmp*'] }]; }")
    try:
        assert page.evaluate("() => __winnow.pluginFormatFor('wtmp')") is None
        assert page.evaluate("() => __winnow.pluginFormatFor('/var/log/btmp.1')") is None
        assert page.evaluate("() => __winnow.pluginFormatFor('tmpfile').id") == "linux"
    finally:
        page.evaluate("() => { __winnow.S.pluginFormats = window.__savedFormats; }")


def test_a_renamed_upload_is_queued_as_login_records_not_skipped(page):
    """A binary no name claims was dropped from the queue under "Skipped 1
    binary file" — a wtmp copied out as evidence.bin included."""
    try:
        kinds = page.evaluate(QUEUE_UPLOAD, [_b64(build("400-be", SESSION[:-1])), "evidence.bin"])
        assert kinds == ["utmp"]
        kinds = page.evaluate(QUEUE_UPLOAD, [_b64(b"\x00\x01\x02" * 400), "blob.bin"])
        assert kinds == []
    finally:
        page.evaluate("() => { __winnow.S.importQueue = []; }")


def test_login_records_preview_and_import_from_the_queue(page, ui_csv):
    p = Path(ui_csv).parent / "wtmp"
    p.write_bytes(build("400-le"))
    try:
        page.evaluate("async (p) => { __winnow.S.importQueue = []; await __winnow.queuePaths([{ path: p, name: 'wtmp' }]); __winnow.openImportModal(); }", str(p))
        page.wait_for_selector("#modal:not([hidden]) .import-pane .preview-tbl")
        assert _hint(page, "wtmp") == "Login records · wtmp"
        assert page.locator("#modalBody .import-readas button").all_inner_texts() == ["Login records", "One line per row"]
        foot = page.locator("#modalBody .import-preview .side-preview-foot").inner_text()
        assert "400-byte records, little-endian (64-bit ARM)" in foot and f"{len(SESSION)} records" in foot
        assert page.locator("#modalBody .preview-colname").all_inner_texts() == utmpread.COLUMNS
        assert page.locator("#modalBody .preview-tbl tr").nth(4).locator("td").nth(2).inner_text() == "root"

        page.locator("#modalBody .import-foot .btn.primary").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'wtmp' && s.row_count === 7 && s.columns.length)", timeout=20_000)
        src = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'wtmp')")
        assert [c["name"] for c in src["columns"]] == utmpread.COLUMNS
        title = page.evaluate("() => __winnow.sourceTitle(__winnow.S.sources.find((s) => s.name === 'wtmp'))")
        assert "Login records — 400-byte records, little-endian (64-bit ARM)" in title
    finally:
        page.evaluate(DROP, "wtmp")


def test_an_uploaded_btmp_previews_with_its_torn_tail(page):
    data = build("384-le", SESSION[3:5]) + b"\x07" * 50
    try:
        page.evaluate("""async ([b64, name]) => {
          const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
          __winnow.S.importQueue = [];
          await __winnow.queueFiles([new File([bytes], name)]);
          __winnow.openImportModal();
        }""", [_b64(data), "btmp.1"])
        page.wait_for_selector("#modal:not([hidden]) .import-pane .preview-tbl")
        assert _hint(page, "btmp.1") == "Login records · btmp"
        foot = page.locator("#modalBody .import-preview .side-preview-foot")
        assert "the last 50 bytes are not a whole record" in foot.inner_text()
        rows = page.locator("#modalBody .preview-tbl tr")
        assert rows.count() == 3   # the heading and two records
        assert rows.nth(1).locator("td").nth(2).inner_text() == ROWS[3][2]
    finally:
        page.evaluate("() => { __winnow.closeModal(); __winnow.S.importQueue = []; }")
