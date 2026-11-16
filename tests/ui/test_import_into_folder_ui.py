"""Choosing the table folder at import, in the two import windows: the
Import window's TABLE FOLDER under its queue, and Import a folder's
"Table folder" row. Analysts asked for it because every import landed at
the top of the sidebar, to be dragged into its host's folder one table at
a time afterwards.

The backend half (every route's folder_id, the job filing the tables,
--folder) is pinned in tests/test_import_into_folder.py. These pin what
only a browser shows: that the choice reaches every request the windows
send, that "New folder…" makes the folder and chooses it, that the tables
really appear in it, and that a folder deleted while it was chosen stops
the import with the queue intact instead of every file failing one by one.

Each test removes the tables and folders it made, by name, in a finally."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

CSV = "When,User\n2026-03-14 08:00:00,alice\n2026-03-14 08:01:00,bob\n"

IDLE = ("() => __winnow.api('/api/ingest/jobs').then((r) =>"
        " !r.jobs.some((j) => j.status === 'running' || j.status === 'queued'))")


def _cleanup(page, api, names, folders):
    page.wait_for_function(IDLE, polling=100, timeout=30_000)
    for s in api("/api/sources"):
        if s["name"] in names:
            api(f"/api/source/{s['id']}", "DELETE")
    for f in api("/api/folders"):
        if f["name"] in folders:
            api(f"/api/folders/{f['id']}", "DELETE")
    page.evaluate("() => { __winnow.closeModal(); __winnow.S.importQueue = []; __winnow.S.importFolderId = null; }")
    page.evaluate("() => __winnow.loadSources()")


def _tables_in(page, names):
    """{name: folder_id} once every one of `names` is listed and no import
    is still running."""
    page.wait_for_function("(names) => names.every((n) => __winnow.S.sources.some((s) => s.name === n))",
                           arg=names, timeout=30_000)
    page.wait_for_function(IDLE, polling=100, timeout=30_000)
    page.evaluate("() => __winnow.loadSources()")
    return page.evaluate("(names) => Object.fromEntries(__winnow.S.sources"
                         ".filter((s) => names.includes(s.name)).map((s) => [s.name, s.folder_id]))", names)


def _folder_id(page, name):
    return page.evaluate("(n) => (__winnow.S.folders.find((f) => f.name === n) || {}).id ?? null", name)


def _new_folder_from(page, select, name):
    select.select_option("new")
    page.wait_for_selector(".confirm-overlay .confirm-input")
    page.locator(".confirm-overlay .confirm-input").fill(name)
    page.locator(".confirm-actions .btn", has_text="Create").click()
    page.wait_for_selector(".confirm-overlay", state="detached")


def test_a_new_folder_made_in_the_import_window_gets_every_queued_table(page, api, tmp_path):
    by_path = tmp_path / "Security.csv"
    by_path.write_text(CSV)
    picked = tmp_path / "System.csv"
    picked.write_text(CSV)
    names = ["Security.csv", "System.csv"]
    try:
        page.evaluate("""async (p) => { __winnow.S.importQueue = [];
          await __winnow.queuePaths([{ path: p, name: 'Security.csv' }]);
          __winnow.openImportModal(); }""", str(by_path))
        page.wait_for_selector("#modal:not([hidden]) .import-pane")
        # One for the queue, beside it — it starts at the top level, as
        # every import did before there was a choice.
        pick = page.locator("#modal .import-into select")
        assert pick.input_value() == ""
        assert pick.locator("option:checked").inner_text() == "No folder (top level)"
        page.locator("#modal input[type=file]").set_input_files(str(picked))
        page.wait_for_selector("#modal .side-rail-item .side-rail-label:text-is('System.csv')")

        _new_folder_from(page, pick, "WS01")
        # Made there and then: chosen, and in the sidebar already.
        page.wait_for_function("() => document.querySelector('#modal .import-into select option:checked')"
                               "?.textContent === 'WS01'")
        page.wait_for_selector("#sidebarList .sidebar-folder .folder-name:text-is('WS01')")
        fid = _folder_id(page, "WS01")
        assert fid is not None

        sent = []
        page.on("request", lambda r: sent.append(r) if "/api/ingest/jobs/" in r.url and r.method == "POST" else None)
        page.locator("#modal .import-foot .btn.primary", has_text="Import 2 files").click()
        filed = _tables_in(page, names)
        # Both transports: the one read in place and the browser upload
        # (whose multipart body Playwright doesn't show, so it is the table
        # that answers for it).
        assert filed == {"Security.csv": fid, "System.csv": fid}
        assert sorted(r.url.rsplit("/", 1)[1] for r in sent) == ["path", "upload"]
        by_path_req = next(r for r in sent if r.url.endswith("/jobs/path"))
        assert by_path_req.post_data_json["folder_id"] == fid
        page.wait_for_function(
            """() => { const f = [...document.querySelectorAll('#sidebarList .sidebar-folder')]
                 .find((x) => x.querySelector('.folder-name')?.textContent === 'WS01');
                 return f && f.querySelector('.sidebar-row-count')?.textContent === '2'; }""")

        # The choice was for that import: the next one starts at the top.
        page.evaluate("() => __winnow.openImportModal()")
        assert page.locator("#modal .import-into select").input_value() == ""
    finally:
        _cleanup(page, api, names, ["WS01"])


def test_import_a_folder_files_its_tree_beneath_the_chosen_folder(page, api, tmp_path):
    root = tmp_path / "DC01-kape"
    (root / "EventLogs").mkdir(parents=True)
    (root / "top.csv").write_text(CSV)
    (root / "EventLogs" / "Security.csv").write_text(CSV)
    names = ["top.csv", "Security.csv"]
    try:
        host = api("/api/folders", "POST", {"name": "DC01", "parent_id": None})
        page.evaluate("() => __winnow.loadSources()")
        # Chosen in the Import window, it carries into Import a folder.
        page.evaluate("() => { __winnow.S.importQueue = []; __winnow.openImportModal(); }")
        page.locator("#modal .import-into select").select_option(str(host["id"]))
        page.locator("#modal .import-foot .btn", has_text="Import a whole folder…").click()
        row = page.locator("#modal .import-into-row")
        row.wait_for()
        assert row.locator("select option:checked").inner_text() == "DC01"

        page.evaluate("(r) => __winnow.openDirectoryImportModal({ root: r, folderId: __winnow.S.folders"
                      ".find((f) => f.name === 'DC01').id })", str(root))
        page.wait_for_selector("#modal .row-actions .btn.primary:text-is('Import checked (2)'):not([disabled])")
        # "New folder…" starts from the scanned folder's name — the host's
        # triage output, usually, which is what the analyst files by.
        page.locator("#modal .import-into-row select").select_option("new")
        page.wait_for_selector(".confirm-overlay .confirm-input")
        assert page.locator(".confirm-overlay .confirm-input").input_value() == "DC01-kape"
        page.locator(".confirm-actions .btn", has_text="Cancel").click()
        page.wait_for_selector(".confirm-overlay", state="detached")
        assert page.locator("#modal .import-into-row select option:checked").inner_text() == "DC01"

        page.locator("#modal .row-actions .btn.primary", has_text="Import checked (2)").click()
        filed = _tables_in(page, names)
        assert filed["top.csv"] == host["id"]
        sub = next(f for f in api("/api/folders") if f["id"] == filed["Security.csv"])
        assert sub["name"] == "EventLogs" and sub["parent_id"] == host["id"]
    finally:
        _cleanup(page, api, names, ["EventLogs", "DC01"])


def test_a_folder_deleted_while_chosen_stops_the_import_and_keeps_the_queue(page, api, tmp_path):
    f = tmp_path / "Security.csv"
    f.write_text(CSV)
    sent = []
    page.route("**/api/ingest/jobs/path", lambda route: (sent.append(route.request), route.abort()))
    try:
        gone = api("/api/folders", "POST", {"name": "Gone", "parent_id": None})
        page.evaluate("""async (p) => { await __winnow.loadSources(); __winnow.S.importQueue = [];
          await __winnow.queuePaths([{ path: p, name: 'Security.csv' }]);
          __winnow.openImportModal(); }""", str(f))
        page.locator("#modal .import-into select").select_option(str(gone["id"]))
        # Deleted elsewhere — the sidebar, another window — while chosen.
        api(f"/api/folders/{gone['id']}", "DELETE")
        page.locator("#modal .import-foot .btn.primary", has_text="Import 1 file").click()
        page.wait_for_selector(".toast:has-text('That table folder has been deleted')")
        # Nothing was sent, the window is still open with the file in it,
        # and the choice is back at the top level, ready to import from.
        assert sent == []
        assert page.locator("#modal:not([hidden]) .side-rail-item .side-rail-label:text-is('Security.csv')").count() == 1
        assert page.locator("#modal .import-into select").input_value() == ""
        assert page.locator("#modal .import-into select option", has_text="Gone").count() == 0
    finally:
        page.unroute("**/api/ingest/jobs/path")
        _cleanup(page, api, ["Security.csv"], ["Gone"])
