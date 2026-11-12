"""Body files in the frontend: recognized by name and by their first line
exactly as the server recognizes them (merge.js keeps twins of
bodyread's name_says and looks_like), asked of the server for a path and
read from the head of an upload, ahead of any plugin that claims .log,
previewed from the queue with their fixed columns, offered as a Read as
choice for delimited text, and imported end to end through it."""
from __future__ import annotations

from pathlib import Path

import pytest

from winnow import bodyread

pytestmark = pytest.mark.ui

PASSWD = "0|/etc/passwd|1835063|r/rrw-r--r--|0|0|2913|1714557598|1714473600|1714473700|1714000000"
NTFS = ("0|C:/Users/alice/AppData/Local/Temp/evil.exe ($FILE_NAME)|102345-48-2|r/rrwxrwxrwx|0|0|72704"
        "|1714557598|1714557598|1714557598|1714557598")
UAC = ("0|/home/alice/.bash_history|2621455|-rw-------|1000|1000|5532"
       "|1714557598.1234567890|1714473600.9999999990|1714473600.0000000000|0")
PIPES = "0|/tmp/a|b|c|1835300|r/rrw-r--r--|0|0|10|1714557598|1714557598|1714557598|0"
TSK2 = "0|/etc/passwd|2049|1835063|33188|-/-rw-r--r--|1|0|0|0|2913|1714557598|1714473600|1714473700|4096|8"
BODY = "\n".join([PASSWD, NTFS, UAC, PIPES]) + "\n"
HEADER = "MD5|name|inode|mode_as_string|UID|GID|size|atime|mtime|ctime|crtime"
OWN_HEADER = "md5|file|st_ino|st_ls|st_uid|st_gid|st_size|st_atime|st_mtime|st_ctime|st_crtime"

# One list through both sides: the server's rule and its merge.js twin
# must agree on every one, the ones that aren't body files included.
TEXTS = [
    PASSWD, NTFS, UAC, PIPES, "\n\n  " + PASSWD, "\ufeff" + PASSWD, PASSWD + "\r\n" + NTFS,
    "d41d8cd98f00b204e9800998ecf8427e|/bin/sh -> dash|1835200|l/lrwxrwxrwx|0|0|4|1700000000|1700000000|1700000000|0",
    "0|/x|1|r/r---------|0|0|0|-|-|-|-", "0|/x|||||||||", "|/x|1|r|0|0|0|1|1|1|1",
    "0|/x|1|r/r---------|0|0|0|-86400|1|1|1",
    "", "\n\n", TSK2,
    HEADER + "\n" + PASSWD, "\ufeff" + HEADER + "\r\n\r\n" + PASSWD, OWN_HEADER + "\n" + PASSWD,
    HEADER, HEADER + "\nfls: error\n" + PASSWD, HEADER + "\n" + HEADER + "\n" + PASSWD,
    "MD5|name|inode|mode|UID|GID|size|atime|mtime|ctime\n" + PASSWD,
    "MD5|name|inode|mode|UID|GID|size|1|mtime|ctime|crtime\n" + PASSWD,
    "path|name|inode|mode|UID|GID|size|atime|mtime|ctime|crtime\n" + PASSWD,
    "Date,Size,Type,Mode,UID,GID,Meta,File Name\nWed May 01 2024 09:59:58,2913,.a..,r/rrw-r--r--,0,0,1835063,/etc/passwd",
    "a|b|c\n1|2|3", "0|/x|1|r|0|0|big|1|1|1|1", "0|/x|1|r|0|0|1|yesterday|1|1|1",
    "zz|/x|1|r|0|0|1|1|1|1|1", "0|/x|1|r|0|0|1|1|1|1", "0|/x|1|r|0|0|1|1e9|1|1|1",
    "0|/x|1|r|0|0|1|١٧١٤٥٥٧٥٩٨|1|1|1",
    "date=2026-10-23 time=05:39:00 devname=FG x=1", "some header line\n" + PASSWD,
]
NAMES = [
    "bodyfile", "bodyfile.txt", "BODYFILE.TXT", "bodyfile (2).txt", "mft.body", "host1.BODY",
    "/mnt/uac/bodyfile/bodyfile.txt", r"C:\cases\uac\bodyfile\bodyfile.txt",
    "body", "body.txt", "bodyfile.gz", "bodyfile.csv", "mybodyfile.txt", "notes.txt", ".body", "x.body.txt",
]


def test_the_client_recognizes_what_the_server_does(page):
    got = page.evaluate("(texts) => texts.map((t) => __winnow.looksLikeBodyfile(t))", TEXTS)
    assert got == [bodyread.looks_like(t) for t in TEXTS]
    assert any(got) and not all(got)
    named = page.evaluate("(names) => names.map((n) => __winnow.bodyfileNamed(n))", NAMES)
    assert named == [bodyread.name_says(n) for n in NAMES]
    assert any(named) and not all(named)
    # A name that says it is one is the body file reader's outright; one
    # under another name is only asked about where kvCandidate says so.
    kinds = page.evaluate("(names) => names.map((n) => __winnow.importKindFor(n))",
                          ["bodyfile", "bodyfile.txt", "mft.body", "fls.txt", "bodyfile.gz"])
    assert kinds == ["bodyfile", "bodyfile", "bodyfile", "csv", "archive"]
    item = page.evaluate("() => __winnow.queueItem({ path: '/x/bodyfile' }, 'bodyfile')")
    assert item["kind"] == "bodyfile" and item["configured"] is True
    # The folder-import chip is on in the defaults, and says what it is.
    assert "bodyfile" in page.evaluate("() => __winnow.defaultImportExtensions()")
    assert page.evaluate("() => __winnow.importTypeLabel('bodyfile')") == "body files"


DROP = """async (names) => {
  __winnow.S.importQueue = [];
  const ss = await __winnow.api('/api/sources');
  for (const s of ss.filter((s) => names.includes(s.name)))
    await __winnow.api('/api/source/' + s.id, { method: 'DELETE' });
  await __winnow.loadSources();
}"""


def _heads(page):
    return page.locator("#modalBody .preview-colname").all_inner_texts()


def _hint(page, name):
    return page.locator("#modalBody .side-rail-item", has_text=name).locator(".side-rail-hint").inner_text()


def _pressed(page, seg):
    return page.locator(f"#modalBody .{seg} button[aria-pressed=true]").get_attribute("data-key")


def _heads_are(page, want):
    page.wait_for_function("(want) => JSON.stringify([...document.querySelectorAll('#modalBody .preview-colname')]"
                           ".map((n) => n.textContent)) === JSON.stringify(want)", arg=want, timeout=10_000)


def test_a_body_file_by_path_previews_and_imports(page, ui_csv):
    """UAC's bodyfile.txt is recognized by its name, an fls listing saved
    as a .txt by its first line (the queue asks the server), and both are
    shown as body files before they import, not as pipe-delimited CSV
    with /etc/passwd's line for a header."""
    d = Path(ui_csv).parent
    (d / "bodyfile.txt").write_text(BODY, newline="")
    (d / "fls-sda1.txt").write_text(BODY, newline="")
    try:
        page.evaluate("""async (d) => {
          __winnow.S.importQueue = [];
          await __winnow.queuePaths([{ path: d + '/bodyfile.txt', name: 'bodyfile.txt' },
                                     { path: d + '/fls-sda1.txt', name: 'fls-sda1.txt' }]);
        }""", str(d))
        assert page.evaluate("() => __winnow.S.importQueue.map((i) => i.kind)") == ["bodyfile", "bodyfile"]
        page.evaluate("() => { __winnow.S.importQueue.pop(); __winnow.openImportModal(); }")
        page.wait_for_selector("#modal:not([hidden]) .import-pane .preview-tbl")
        assert _hint(page, "bodyfile.txt") == "Body file · a row per file"
        assert _pressed(page, "import-readas") == "bodyfile"
        assert page.locator("#modalBody .import-readas button").all_inner_texts() == ["Body file", "Delimited", "One line per row"]
        assert _heads(page) == bodyread.COLUMNS
        foot = page.locator("#modalBody .import-preview .side-preview-foot").inner_text()
        assert foot.startswith("Body file · first 4 rows")
        # The name with pipes in it is one cell, and the times are UTC text.
        assert page.locator("#modalBody .preview-tbl td", has_text="/tmp/a|b|c").count() == 1
        assert page.locator("#modalBody .preview-tbl td", has_text="2024-04-30 10:40:00.999999").count() == 1

        page.locator("#modalBody .import-foot .btn.primary").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'bodyfile.txt' && s.row_count === 4 && s.columns.length)", timeout=20_000)
        cols = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'bodyfile.txt').columns")
        assert [(c["name"], c["type"]) for c in cols] == list(zip(bodyread.COLUMNS, bodyread.COLUMN_TYPES))
    finally:
        page.evaluate(DROP, ["bodyfile.txt"])


def test_an_uploaded_body_file_is_recognized_from_its_head(page):
    """A browser-picked file is recognized in the browser, from the same
    first bytes the server would read, and can be read one line per row
    and back."""
    try:
        page.evaluate("""async (text) => {
          __winnow.S.importQueue = [];
          await __winnow.queueFiles([new File([text], 'host1-fls.txt'), new File(['a|b\\n1|2\\n'], 'other.txt')]);
        }""", BODY)
        assert page.evaluate("() => __winnow.S.importQueue.map((i) => i.kind)") == ["bodyfile", "csv"]
        page.evaluate("() => { __winnow.S.importQueue.pop(); __winnow.openImportModal(); }")
        page.wait_for_selector("#modal:not([hidden]) .import-pane .preview-tbl")
        _heads_are(page, bodyread.COLUMNS)
        page.locator("#modalBody .import-readas button[data-key=text]").click()
        _heads_are(page, ["Message"])
        assert _hint(page, "host1-fls.txt") == "Text · one line per row"
        page.locator("#modalBody .import-readas button[data-key=bodyfile]").click()
        _heads_are(page, bodyread.COLUMNS)
        page.locator("#modalBody .import-foot .btn.primary").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'host1-fls.txt' && s.row_count === 4 && s.columns.length)", timeout=20_000)
    finally:
        page.evaluate(DROP, ["host1-fls.txt"])


def test_a_body_file_with_a_header_line_keeps_its_headers(page):
    """A header line above the first file is recognized as the start of a
    body file, and its names are the table's headers, not a row."""
    try:
        page.evaluate("""async (text) => {
          __winnow.S.importQueue = [];
          await __winnow.queueFiles([new File([text], 'listing.txt')]);
          __winnow.openImportModal();
        }""", OWN_HEADER + "\n" + BODY)
        page.wait_for_selector("#modal:not([hidden]) .import-pane .preview-tbl")
        assert page.evaluate("() => __winnow.S.importQueue[0].kind") == "bodyfile"
        _heads_are(page, OWN_HEADER.split("|"))
        foot = page.locator("#modalBody .import-preview .side-preview-foot").inner_text()
        assert foot.startswith("Body file · first 4 rows") and "isn’t a body file line" not in foot
    finally:
        page.evaluate(DROP, ["listing.txt"])


def test_delimited_text_can_be_read_as_a_body_file(page):
    """A body file whose first line didn't say so (fls's own error on the
    first line, here) is still one choice away, and the line that isn't
    one is kept whole in Unparsed line."""
    text = "fls: Error reading inode 12\n" + BODY
    try:
        page.evaluate("""async (text) => {
          __winnow.S.importQueue = [];
          await __winnow.queueFiles([new File([text], 'fls-out.txt')]);
          __winnow.openImportModal();
        }""", text)
        page.wait_for_selector("#modal:not([hidden]) .import-pane .preview-tbl")
        assert page.evaluate("() => __winnow.S.importQueue[0].kind") == "csv"
        assert page.locator("#modalBody .import-readas button").all_inner_texts() == [
            "Delimited", "One line per row", "Key=value", "Body file"]
        page.locator("#modalBody .import-readas button[data-key=bodyfile]").click()
        _heads_are(page, bodyread.COLUMNS + ["Unparsed line"])
        assert _hint(page, "fls-out.txt") == "Body file · a row per file"
        assert "1 line isn’t a body file line" in page.locator("#modalBody .import-preview .side-preview-foot").inner_text()
        page.locator("#modalBody .import-foot .btn.primary").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'fls-out.txt' && s.row_count === 5 && s.columns.length)", timeout=20_000)
        src = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'fls-out.txt')")
        assert src["columns"][-1]["name"] == "Unparsed line"
    finally:
        page.evaluate(DROP, ["fls-out.txt"])


def test_a_plugin_claiming_log_does_not_take_a_body_file(page):
    page.evaluate("() => { window.__savedFormats = __winnow.S.pluginFormats;"
                  " __winnow.S.pluginFormats = [{ id: 'esxi', label: 'ESXi', extensions: ['.log'], filename_patterns: ['body*'] }]; }")
    try:
        kinds = page.evaluate("""async (text) => {
          __winnow.S.importQueue = [];
          await __winnow.queueFiles([new File([text], 'fls.log'), new File([text], 'bodyfile'),
                                     new File(['hostd started\\n'], 'hostd.log')]);
          return __winnow.S.importQueue.map((i) => i.kind);
        }""", BODY)
        assert kinds == ["bodyfile", "bodyfile", "plugin"]
    finally:
        page.evaluate("() => { __winnow.S.pluginFormats = window.__savedFormats; __winnow.S.importQueue = []; }")
