"""The background-jobs pipeline as the analyst sees it: a job that errors
must announce itself. Everything below drives the REAL poll loop — the
job is started over the same route the import modal uses, and the toast
is the one an analyst would read."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_a_failing_import_job_toasts_its_error(page, tmp_path):
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    before = page.evaluate("() => __winnow.S.sources.length")
    # post-then-poll is exactly what the import modal does. The job
    # errors instantly, so this doubles as the regression test for the
    # first-poll dismissal bug: a fresh page's first fast-failing job
    # used to be filed as pre-existing history and produced NO toast.
    page.evaluate(
        """(path) => __winnow.post('/api/ingest/jobs/path',
             { path, name: 'empty.csv', kind: 'csv' })
           .then(() => __winnow.startJobsPoll())""",
        str(empty))
    page.wait_for_function(
        """() => { const t = document.getElementById('toast');
                   return t && !t.hidden && t.textContent.includes('Import failed'); }""",
        timeout=15_000)
    toast = page.locator("#toast").inner_text()
    assert "empty" in toast.lower()
    # And the failure produced no phantom source.
    page.evaluate("() => __winnow.loadSources()")
    page.wait_for_timeout(300)
    assert page.evaluate("() => __winnow.S.sources.length") == before


def test_one_failed_poll_does_not_stop_the_import_being_followed(page, tmp_path):
    """A single failed /api/ingest/jobs (a 500 while the server was busy, a
    blink in the connection) emptied the jobs list and, with nothing left
    "running", stopped the poll for good: the progress row vanished, the
    finished import was never announced, and its table never reached the
    tab strip or the sidebar until the page was reloaded. The FIRST poll
    after starting the import is the one that fails here, so the import
    cannot have been seen running before the failure."""
    csv = tmp_path / "after_a_blink.csv"
    csv.write_text("Host,User\nh1,alice\nh2,bob\n", encoding="utf-8")
    failed = []

    def one_failure(route):
        if not failed:
            failed.append(route.request.url)
            route.fulfill(status=500, content_type="application/json", body='{"detail": "boom"}')
        else:
            route.continue_()

    page.route("**/api/ingest/jobs", one_failure)
    try:
        page.evaluate(
            """(path) => __winnow.post('/api/ingest/jobs/path',
                 { path, name: 'after_a_blink.csv', kind: 'csv' })
               .then(() => __winnow.startJobsPoll())""",
            str(csv))
        page.wait_for_function(
            "() => __winnow.S.sources.some((s) => s.name === 'after_a_blink.csv')", timeout=20_000)
        assert failed, "the injected failure never happened, so this proved nothing"
    finally:
        page.unroute("**/api/ingest/jobs")
        # The session's case is shared — leave it as it was found.
        page.evaluate("""() => fetch('/api/sources').then((r) => r.json()).then((ss) => Promise.all(
            ss.filter((s) => s.name === 'after_a_blink.csv').map((s) => fetch('/api/source/' + s.id,
              { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } }))))""")
