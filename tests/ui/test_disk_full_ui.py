"""A full disk in the jobs panel (winnow/diskspace.py, server.py's 507).

The report was an import that "failed silently" when the analyst's drive
was full. In the browser that looked like this: the upload row vanished
from the corner panel, a toast said "Upload failed for big.csv: There was
an error parsing the body" for six seconds, and no table arrived. An
analyst who had looked away for those six seconds saw nothing at all.

So a failed upload now leaves an error row behind, as a failed import job
always has, and a full disk is named on the badge, with one line under it
(the drive, and what was kept) and a four-word toast — the first version
put every drive and what to do in both, and that was too much to read in
a corner. The server's half (the 507, its line, the log) is
tests/test_disk_full.py; here the upload is
answered by page.route with what that server sends, since filling the
shared server's disk is not something a test can do to its neighbours."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

PANEL = "#jobsPanel:not([hidden])"
# A finished row that failed: the same file's "uploading" or "indexing"
# row can be in the panel beside it, carrying the same name.
FAILED_ROW = f"{PANEL} .job-row:has(.job-phase.error)"
MESSAGE = "Out of disk space on C: (6.0 MB free). The 10.4 MB file was not received."


def _upload(req):
    return req.method == "POST" and req.url.endswith("/api/ingest/jobs/upload")


@pytest.fixture
def quiet_panel(page):
    """Only this test's rows in the panel (test_clear_all_notifications.py
    explains the save-and-restore), and the import dialog closed after."""
    page.evaluate("""() => {
      window.__realJobs = __winnow.ingestJobs.slice();
      __winnow.ingestJobs.length = 0;
      for (const id of [...__winnow.pluginNotices.keys()]) __winnow.closeNotice(id);
      __winnow.renderJobsPanel();
    }""")
    # The one-time Open With offer follows a browser import of a type not
    # yet offered (assoc.js maybeOfferAssociation), and answering it would
    # change the shared server's workspace for every module after this
    # one. Told there is nothing to offer, it asks nothing.
    page.route("**/api/assoc/types", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"platform": "other", "types": []})))
    yield
    page.unroute("**/api/assoc/types")
    page.unroute("**/api/ingest/jobs/upload")
    page.evaluate("""() => {
      __winnow.closeModal();
      __winnow.S.importQueue = [];
      for (const id of [...__winnow.pluginNotices.keys()]) __winnow.closeNotice(id);
      __winnow.ingestJobs.length = 0;
      __winnow.ingestJobs.push(...(window.__realJobs || []));
      delete window.__realJobs;
      __winnow.renderJobsPanel();
    }""")


def _pick_and_import(page, tmp_path, status, detail):
    page.route("**/api/ingest/jobs/upload", lambda route: route.fulfill(
        status=status, content_type="application/json", body=json.dumps({"detail": detail})))
    path = tmp_path / "big.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    page.evaluate("() => __winnow.openImportModal()")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modal input[type=file]").set_input_files(str(path))
    page.wait_for_selector("#modal .side-rail-item .side-rail-label:text-is('big.csv')")
    with page.expect_request(_upload):
        page.locator("#modal .import-foot .btn.primary", has_text="Import 1 file").click()


def test_an_upload_the_disk_could_not_hold_stays_in_the_panel(page, tmp_path, quiet_panel):
    _pick_and_import(page, tmp_path, 507, MESSAGE)
    row = page.locator(FAILED_ROW, has_text="big.csv")
    row.wait_for()
    # Named on the badge, in the error color, with the server's line
    # under it: which drive, how much is free.
    badge = row.locator(".job-phase")
    assert badge.inner_text().lower() == "out of space"
    assert "error" in badge.get_attribute("class").split()
    assert row.locator(".job-detail").inner_text() == MESSAGE
    # The toast says it in four words, and no more: the row has the rest.
    page.wait_for_function("(m) => document.getElementById('toast').textContent === m",
                           arg="Upload failed for big.csv: out of disk space")
    # Stays until it is dismissed — then goes.
    row.locator(".job-x[title=Dismiss]").click()
    page.wait_for_function("() => ![...document.querySelectorAll('#jobsPanel .job-row')]"
                           ".some((r) => r.textContent.includes('big.csv'))")


def test_any_failed_upload_leaves_a_row(page, tmp_path, quiet_panel):
    """Not only a full disk: an upload the server refused for any reason
    used to leave nothing but the toast."""
    _pick_and_import(page, tmp_path, 400, "Unknown ingest kind: nope")
    row = page.locator(FAILED_ROW, has_text="big.csv")
    row.wait_for()
    assert row.locator(".job-phase").inner_text().lower() == "upload failed"
    assert row.locator(".job-detail").inner_text() == "Unknown ingest kind: nope"


def test_an_import_job_the_disk_stopped_says_out_of_space(page, quiet_panel):
    """The job's own row, from the poll's record: error_kind names the
    cause, and the badge says it instead of a bare ERROR."""
    msg = "Out of disk space on D: (0 B free). 40,000 rows were kept as a partial table."
    page.evaluate("""(msg) => {
      __winnow.ingestJobs.push(
        { job_id: 991, name: 'evtx.csv', kind: 'csv', status: 'error', error: msg, error_kind: 'disk_full' },
        { job_id: 992, name: 'other.csv', kind: 'csv', status: 'error', error: 'Line 3: bad', error_kind: null });
      __winnow.renderJobsPanel();
    }""", msg)
    full = page.locator(FAILED_ROW, has_text="evtx.csv")
    full.wait_for()
    assert full.locator(".job-phase").inner_text().lower() == "out of space"
    assert full.locator(".job-detail").inner_text() == msg
    other = page.locator(FAILED_ROW, has_text="other.csv")
    assert other.locator(".job-phase").inner_text().lower() == "error"


def test_the_partial_table_a_failed_import_kept_appears(page, api, until, tmp_path, quiet_panel):
    """"Kept as a partial table", says the message — and the table did not
    show up in the list until something else happened to refresh it, long
    after the message had gone. A real import is reported to the page as
    failed (the way the server reports one the disk stopped: status error,
    no source_ids), and the list has to pick its table up anyway."""
    name = "kept-partial.csv"
    path = tmp_path / name
    path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")

    def as_failed(route):
        resp = route.fetch()
        body = resp.json()
        for j in body["jobs"]:
            if j["name"] == name:
                j.update(status="error", error_kind="disk_full", source_ids=[], result=None,
                         error="Out of disk space on D: (0 B free). 2 rows were kept as a partial table.")
        route.fulfill(response=resp, json=body)

    page.route("**/api/ingest/jobs", as_failed)
    try:
        page.evaluate("""(p) => __winnow.post('/api/ingest/jobs/path', { path: p, kind: 'csv' })
                          .then(() => __winnow.startJobsPoll())""", str(path))
        page.locator(FAILED_ROW, has_text=name).wait_for()
        page.wait_for_function("(n) => __winnow.S.sources.some((s) => s.name === n)", arg=name, timeout=15_000)
    finally:
        page.unroute("**/api/ingest/jobs")
        until(page, lambda: not any(j["status"] in ("running", "queued")
                                    for j in api("/api/ingest/jobs")["jobs"]),
              "the import finished", timeout=30)
        for s in api("/api/sources"):
            if s["name"] == name:
                api(f"/api/source/{s['id']}", "DELETE")
        page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
