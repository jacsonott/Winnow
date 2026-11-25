"""Gzipped single files in the import window: routed by the name inside
them exactly as the server routes them (merge.js keeps a twin of
gzipped.inner_name), recognized by what is inside an upload as well —
the browser decompresses the head itself — said to be gzip in the queue
and the file's pane, offered the archive reading for a tarball that hides
under a .gz name, and imported end to end both ways.

The backend half is tests/test_gzip_import.py; what only a browser can
answer is here: DecompressionStream really inflating a picked file's
head, and the preview under the pane being the INNER file's.
"""
from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest

from winnow import gzipped

pytestmark = pytest.mark.ui

FORTI = ("date=2026-10-23 time=05:39:00 devid=FG6H1ETB20902601 devname=Main-Firewall "
         'type=traffic subtype=forward srcip=192.168.1.50 dstintf="wan1" action=accept')

NAMES = [
    "events.json.gz", "events.JSON.GZ", "hosts.tsv.gz", "auth.log.1.gz", "messages.gz",
    "u_ex240501.log.gz", "wtmp.gz", "bodyfile.txt.gz", "History.db.gz", "book.xlsx.gz",
    "bundle.tar.gz", "bundle.TAR.gz", "bundle.tgz", ".gz", "events.json", "events.zip", "",
    "/cases/42/events.csv.gz", r"C:\cases\events.csv.gz",
]

DROP = """async (name) => {
  __winnow.S.importQueue = [];
  const ss = await __winnow.api('/api/sources');
  for (const s of ss.filter((s) => s.name === name))
    await __winnow.api('/api/source/' + s.id, { method: 'DELETE' });
  await __winnow.loadSources();
}"""


def _hint(page, name):
    return page.locator("#modalBody .side-rail-item", has_text=name).locator(".side-rail-hint").inner_text()


def _heads(page):
    return page.locator("#modalBody .preview-colname").all_inner_texts()


def _gz(path: Path, text: str) -> Path:
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=1654084800) as g:
        g.write(text.encode())
    return path


def test_the_client_routes_by_the_name_inside_as_the_server_does(page):
    got = page.evaluate("(names) => names.map((n) => __winnow.gzipInner(n))", NAMES)
    want = [gzipped.inner_name(n) for n in NAMES]
    # The browser only ever needs the base name; the server keeps the
    # directories. Compared as base names, the rule must be identical.
    assert got == [None if w is None else w.replace("\\", "/").split("/")[-1] for w in want]
    assert any(got) and not all(got)
    kinds = page.evaluate("""(names) => names.map((n) => __winnow.importKindFor(n))""", [
        "events.json.gz", "hosts.tsv.gz", "events.csv.gz", "u_ex240501.log.gz", "wtmp.gz",
        "bodyfile.txt.gz", "auth.log.1.gz", "History.db.gz", "book.xlsx.gz", "timeline.plaso.gz",
        "hostd.0.gz", "bundle.tar.gz", "bundle.tgz",
    ])
    assert kinds == ["json", "csv", "csv", "iis", "utmp", "bodyfile", "secure", "sqlite", "xlsx",
                     "plaso", "text", "archive", "archive"]


def test_an_uploaded_gz_is_recognised_from_what_is_inside_it(page):
    """The browser inflates the head itself (DecompressionStream), so a
    FortiGate log compressed for mailing is queued as a key=value log and
    not as one line per row — and a gzipped binary is still refused."""
    payloads = {
        "fw.gz": gzip.compress((FORTI + "\n").encode()),
        "wtmp.gz": gzip.compress(bytearray(384 * 3)),       # all-EMPTY slots: not login records
        "tool.exe.gz": gzip.compress(b"MZ\x90\x00" + bytes(4096)),
    }
    kinds = page.evaluate("""async (files) => {
      __winnow.S.importQueue = [];
      await __winnow.queueFiles(files.map(([n, b]) => new File([new Uint8Array(b)], n)));
      return __winnow.S.importQueue.map((i) => [i.name, i.kind]);
    }""", [[n, list(b)] for n, b in payloads.items()])
    # fw.gz by its first line; wtmp.gz by its NAME (an empty wtmp is still
    # a wtmp); the gzipped executable skipped as binary, which is what it
    # is inside — the compressed bytes alone are NULs either way.
    assert kinds == [["fw.gz", "kv"], ["wtmp.gz", "utmp"]]
    page.evaluate("() => { __winnow.S.importQueue = []; }")


def test_a_gz_upload_previews_and_imports_as_the_file_inside_it(page):
    body = "Host,Note\nws01,west\nws02,east\n"
    try:
        page.evaluate("""async (bytes) => {
          __winnow.S.importQueue = [];
          await __winnow.queueFiles([new File([new Uint8Array(bytes)], 'hosts.csv.gz')]);
          __winnow.openImportModal();
        }""", list(gzip.compress(body.encode())))
        page.wait_for_selector("#modal:not([hidden]) .import-pane .preview-tbl")
        # The queue says how it will be read, and that it is compressed.
        assert _hint(page, "hosts.csv.gz") == "Delimited · auto-detect · gzip"
        assert "gzip" in page.locator("#modalBody .import-file-how").inner_text()
        assert "read as hosts.csv" in page.locator("#modalBody .import-note").inner_text()
        # The preview is of the CSV inside, not of compressed bytes.
        assert _heads(page) == ["Host", "Note"]
        assert page.locator("#modalBody .preview-tbl td", has_text="west").count() == 1
        # An archive reading is offered, for a tarball under a .gz name.
        assert page.locator("#modalBody .import-readas button").all_inner_texts() \
            == ["Delimited", "One line per row", "Key=value", "Body file", "Secure log", "Archive"]

        page.locator("#modalBody .import-foot .btn.primary").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'hosts.csv.gz'"
                              " && s.row_count === 2 && s.columns.length === 2)", timeout=20_000)
        cols = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'hosts.csv.gz').columns")
        assert [c["name"] for c in cols] == ["Host", "Note"]
    finally:
        page.evaluate(DROP, "hosts.csv.gz")


def test_a_gz_read_in_place_previews_and_imports_too(page, ui_csv):
    d = Path(ui_csv).parent
    _gz(d / "events.json.gz", json.dumps([{"user": "dana", "action": "logon"},
                                          {"user": "sam", "action": "logoff"}]))
    try:
        page.evaluate("""async (d) => {
          __winnow.S.importQueue = [];
          await __winnow.queuePaths([{ path: d + '/events.json.gz', name: 'events.json.gz' }]);
          __winnow.openImportModal();
        }""", str(d))
        assert page.evaluate("() => __winnow.S.importQueue[0].kind") == "json"
        page.wait_for_selector("#modal:not([hidden]) .import-pane .preview-tbl")
        assert _hint(page, "events.json.gz") == "JSON · nested kept as JSON · gzip"
        assert _heads(page) == ["user", "action"]
        page.locator("#modalBody .import-foot .btn.primary").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'events.json.gz'"
                              " && s.row_count === 2)", timeout=20_000)
        src = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'events.json.gz')")
        # The table says it came from the .gz, not from a temporary copy.
        assert src["path"].endswith("events.json.gz")
    finally:
        page.evaluate(DROP, "events.json.gz")


def test_a_tarball_under_a_gz_name_is_queued_as_an_archive(page, ui_csv):
    """Only the server can see this one (the name says nothing), so the
    queue asks it as it queues a file read in place."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("var/log/auth.log")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"now\n"))
    d = Path(ui_csv).parent
    (d / "collection.gz").write_bytes(buf.getvalue())
    try:
        kind = page.evaluate("""async (d) => {
          __winnow.S.importQueue = [];
          await __winnow.queuePaths([{ path: d + '/collection.gz', name: 'collection.gz' }]);
          return __winnow.S.importQueue[0].kind;
        }""", str(d))
        assert kind == "archive"
        # Nothing about it is called gzip in the queue: it is an archive.
        hint = page.evaluate("() => __winnow.queueHint(__winnow.S.importQueue[0])")
        assert hint == "Archive · expands on import"
    finally:
        page.evaluate("() => { __winnow.S.importQueue = []; }")


def test_the_jobs_panel_says_decompressing(page):
    row = page.evaluate("""() => {
      const j = { job_id: 1, kind: 'csv', name: 'big.csv.gz', status: 'running', decompressing: true };
      return [__winnow.runningJobPhase(j), __winnow.runningJobPhase({ ...j, decompressing: false })];
    }""")
    assert row == ["decompressing", "importing"]
