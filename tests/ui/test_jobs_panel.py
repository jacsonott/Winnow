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
