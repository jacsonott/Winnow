"""Importing through the browser rather than by server path: a file chosen
in the Import dialog's "Upload from browser…" picker, and a file dropped
on the window. Every other UI test imports by path
(/api/ingest/jobs/path), so the upload half of the import loop,
uploadWithProgress and the window's drop handler had never run in a test.

Each test removes the table it imported, by name, in a finally."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

CSV = ("When,User,Payload\n"
       "2026-03-14 08:00:00,alice,\"{\"\"pid\"\": 1}\"\n"
       "2026-03-14 08:01:00,bob,\"{\"\"pid\"\": 2}\"\n"
       "2026-03-14 08:02:00,carol,\"{\"\"pid\"\": 3}\"\n")


def _upload(req):
    return req.method == "POST" and req.url.endswith("/api/ingest/jobs/upload")


IDLE = ("() => __winnow.api('/api/ingest/jobs').then((r) =>"
        " !r.jobs.some((j) => j.status === 'running' || j.status === 'queued'))")


def _wait_for_table(page, api, name):
    """The server's record of the imported table, once every import job has
    finished and the sidebar lists it."""
    page.wait_for_function("(name) => __winnow.S.sources.some((s) => s.name === name)", arg=name, timeout=30_000)
    page.wait_for_function(IDLE, polling=100, timeout=30_000)
    return next(s for s in api("/api/sources") if s["name"] == name)


def _drop_table(page, api, name, home):
    # A test that failed early can leave its import still running, and a
    # table that lands after the delete would outlive the module.
    page.wait_for_function(IDLE, polling=100, timeout=30_000)
    for s in api("/api/sources"):
        if s["name"] == name:
            api(f"/api/source/{s['id']}", "DELETE")
    page.evaluate("() => { __winnow.closeModal(); __winnow.S.importQueue = []; }")
    page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", home)
    page.wait_for_function("(name) => !__winnow.S.sources.some((s) => s.name === name)", arg=name)


def _assert_imported(src):
    assert src["row_count"] == 3
    assert [c["name"] for c in src["columns"]] == ["When", "User", "Payload"]


def test_a_file_picked_in_the_import_dialog_is_uploaded_and_imported(page, api, tmp_path):
    home = page.evaluate("() => __winnow.S.sourceId")
    path = tmp_path / "picked.csv"
    path.write_text(CSV, encoding="utf-8")
    try:
        page.evaluate("() => __winnow.openImportModal()")
        page.wait_for_selector("#modal:not([hidden])")
        page.locator("#modal input[type=file]").set_input_files(str(path))
        page.wait_for_selector("#modal .session-row .session-name:text-is('picked.csv')")
        # Queued as a browser file, not a path on this machine.
        assert "by path" not in page.locator("#modal .session-row").inner_text()
        with page.expect_request(_upload) as req:
            page.locator("#modal button", has_text="Import all queued").click()
        assert req.value.headers["content-type"].startswith("multipart/form-data")
        _assert_imported(_wait_for_table(page, api, "picked.csv"))
    finally:
        _drop_table(page, api, "picked.csv", home)


def test_a_file_dropped_on_the_window_is_queued_uploaded_and_imported(page, api):
    home = page.evaluate("() => __winnow.S.sourceId")
    try:
        # A real drag carries the file in a DataTransfer; the handlers on
        # window read it from there, so the same object goes through the
        # whole enter → over → drop sequence.
        page.evaluate("""(text) => {
            window.__drop = new DataTransfer();
            window.__drop.items.add(new File([text], 'dropped.csv', { type: 'text/csv' }));
            const fire = (type) => document.body.dispatchEvent(
                new DragEvent(type, { dataTransfer: window.__drop, bubbles: true, cancelable: true }));
            fire('dragenter');
            fire('dragover');
        }""", CSV)
        assert page.locator("#dropOverlay").is_visible(), "dragging a file over the case says it can be dropped"
        dropped = page.evaluate("""() => {
            const e = new DragEvent('drop', { dataTransfer: window.__drop, bubbles: true, cancelable: true });
            document.body.dispatchEvent(e);
            return e.defaultPrevented;
        }""")
        # Not prevented, the browser would navigate away to the file.
        assert dropped
        assert not page.locator("#dropOverlay").is_visible()
        page.wait_for_selector("#modal:not([hidden]) .session-row .session-name:text-is('dropped.csv')")
        with page.expect_request(_upload):
            page.locator("#modal button", has_text="Import all queued").click()
        _assert_imported(_wait_for_table(page, api, "dropped.csv"))
    finally:
        page.evaluate("() => { delete window.__drop; }")
        _drop_table(page, api, "dropped.csv", home)
