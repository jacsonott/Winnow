"""A derived column being built says so in the jobs panel (#528).

The report: add a derived column to a table and the jobs panel in the
bottom-right corner says IMPORTING, as if a second copy of the table were
coming in. A derive runs on the import machinery (`Store.start_ingest_job`
with kind 'derive') for its progress bar, its cancel and this panel, and
`renderJobsPanel` gave every running job the same 'importing' badge.

The job lists here are served in place of `/api/ingest/jobs`, so the rows
come through the real poll (`pollJobs` assigns the answer wholesale and
`renderJobsPanel` draws it) without depending on a real pass being slow
enough for a poll to catch it mid-way: over the shared fixture's tables a
derive finishes inside one poll interval, and the badge it wore while it
ran is never on screen. The jobs never finish, so nothing toasts, and once
the route is gone the next poll gets the server's real list and stops.
"""

from __future__ import annotations

import json
import time

import pytest

pytestmark = pytest.mark.ui

PANEL = "#jobsPanel:not([hidden])"


def _job(job_id, name, kind, status="running"):
    return {
        "job_id": job_id, "kind": kind, "name": name, "status": status,
        "rows_done": 5000 if status == "running" else 0,
        "units_done": 50, "units_total": 100,
        "unit": "rows" if kind == "derive" else "bytes",
        "tables_done": 0, "tables_total": 0, "current_table": None,
        "source_ids": [], "result": None, "error": None, "cancelled": False,
        "started_at": time.time(), "finished_at": None,
    }


@pytest.fixture
def serve_jobs(page):
    """Serve `jobs` as the server's job list until the test ends."""
    served = {"jobs": []}

    def handler(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(served))

    page.route("**/api/ingest/jobs", handler)

    def serve(*jobs):
        served["jobs"] = list(jobs)
        page.evaluate("() => __winnow.startJobsPoll()")

    try:
        yield serve
    finally:
        page.unroute("**/api/ingest/jobs")
        # One real poll, so the panel goes back to what the server says
        # rather than keeping the last served list on screen.
        page.evaluate("() => __winnow.startJobsPoll()")
        page.wait_for_function(
            "() => !__winnow.ingestJobs.some((j) => j.job_id >= 9000)", timeout=10_000)


def _row(page, name):
    return page.locator(f"{PANEL} .job-row", has=page.locator(".job-name", has_text=name))


def test_a_running_derive_says_deriving_not_importing(page, serve_jobs):
    serve_jobs(_job(9001, "TimeCreated (UTC)", "derive"))
    row = _row(page, "TimeCreated (UTC)")
    row.wait_for()
    badge = row.locator(".job-phase")
    assert badge.inner_text().strip().lower() == "deriving"
    assert "importing" not in badge.get_attribute("class")
    # Still a running job in every other respect: a bar, and a ✕ that
    # cancels it.
    assert row.locator(".job-bar").count() == 1
    assert row.locator(".job-x").get_attribute("title") == "Cancel"


def test_an_import_beside_it_still_says_importing(page, serve_jobs):
    """The same panel holding both, so the badge is shown to follow each
    job's kind rather than having been renamed for everyone."""
    serve_jobs(_job(9002, "Security.csv", "csv"), _job(9003, "Payload (Data)", "derive"))
    _row(page, "Payload (Data)").wait_for()
    assert _row(page, "Security.csv").locator(".job-phase").inner_text().strip().lower() == "importing"
    assert _row(page, "Payload (Data)").locator(".job-phase").inner_text().strip().lower() == "deriving"


@pytest.mark.parametrize("kinds, detail", [
    (("derive", "derive"), "waiting to derive"),
    (("csv", "json"), "waiting to import"),
    (("csv", "derive"), "waiting to import or derive"),
])
def test_the_queued_summary_names_what_is_waiting(page, serve_jobs, kinds, detail):
    """Column builds and imports share the two slots
    (`MAX_CONCURRENT_INGESTS`), so a column added while big imports run
    waits as queued, and was summed up as "waiting to import" too."""
    serve_jobs(_job(9010, "Busy.csv", "csv"),
               *[_job(9011 + i, f"waiting{i}", k, "queued") for i, k in enumerate(kinds)])
    row = _row(page, f"{len(kinds)} queued")
    row.wait_for()
    assert row.locator(".job-detail").inner_text().strip() == detail
