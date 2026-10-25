"""The jobs panel's Clear all, and the two things it refuses to clear.

A folder import queues one job per file. The server keeps the last
`INGEST_JOB_KEEP` (20) finished jobs, so the pile tops out at twenty rows
rather than one per file — and twenty ✕ clicks per folder is still twenty.
The failures are what stay: a done row auto-dismisses after 8 s, an
errored one never has. The header button clears the lot in one.

What it will NOT clear is the part worth testing, because both refusals
are the kind that look like bugs until you hit the case they exist for:

* **Work still running or queued stays.** Those rows wear a ✕ that means
  Cancel — `jobPanelRow` gives `onCancel` and `onDismiss` the same glyph
  — so a Clear all that treated them alike would silently kill a
  half-finished folder import. That is a far worse outcome than the
  clicking it was meant to save.
* **A finished row still carrying buttons stays.** A background search
  that has landed holds its rows server-side until Apply or Discard, and
  "clear my notifications" is not an answer to that question. An error
  row is never asking anything, so it goes even when a button outlived
  the failure.

The import rows here are written straight into `ingestJobs`. A real
twenty-file import would take minutes and make the assertion about timing
rather than about clearing; the records are the shape the poll builds
(`pollJobs` assigns `d.jobs` wholesale), and the row each one produces is
rendered by the same `renderJobsPanel` the real thing uses. The counts
stay at or under `INGEST_JOB_KEEP` so every state asserted here is one the
server can actually produce.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

PANEL = "#jobsPanel:not([hidden])"
ROWS = f"{PANEL} .job-row"
CLEAR = f"{PANEL} .jobs-clear-btn"
COUNT = f"{PANEL} .jobs-clear-count"


@pytest.fixture(autouse=True)
def quiet_panel(page):
    """A panel holding nothing but what the test puts in it.

    The server is shared across the whole UI run, so by the time this
    module gets a page the job list can already carry finished imports
    other modules made. Empty it and close any plugin notices, then put
    both back, so each test reasons about its own rows only.

    `dismissedJobs` is saved and restored rather than cleared, which is
    the point of the pairing: booting the page dismisses every job that
    finished before it loaded (the `history` branch in pollJobs), and
    that is the whole reason those older imports are not already on
    screen. Clearing the set un-dismisses them, so the real pile paints
    itself back over the grid — a 300px panel across the rows — the
    moment anything repaints the panel.

    What this does NOT do is stop the poll, despite where the rows come
    from, because nothing here can: `jobsPollTimer` lives in jobs.js and
    the namespace object these tests reach through exposes getters only.
    It is survivable because pollJobs re-arms itself only while something
    is uploading, importing, or being watched for an index build, and
    this fixture leaves none of those behind. Were one to land anyway it
    would take the rows below with it — it assigns `ingestJobs = d.jobs`
    wholesale, a rebind, so an in-place push is simply dropped rather
    than merged.
    """
    page.evaluate("""() => {
      window.__realJobs = __winnow.ingestJobs.slice();
      window.__realDismissed = [...__winnow.dismissedJobs];
      __winnow.ingestJobs.length = 0;
      for (const id of [...__winnow.pluginNotices.keys()]) __winnow.closeNotice(id);
      __winnow.renderJobsPanel();
    }""")
    yield
    page.evaluate("""() => {
      for (const id of [...__winnow.pluginNotices.keys()]) __winnow.closeNotice(id);
      __winnow.ingestJobs.length = 0;
      __winnow.ingestJobs.push(...(window.__realJobs || []));
      __winnow.dismissedJobs.clear();
      for (const id of (window.__realDismissed || [])) __winnow.dismissedJobs.add(id);
      delete window.__realJobs;
      delete window.__realDismissed;
      __winnow.renderJobsPanel();
    }""")


def _jobs(page, *specs):
    """specs are (job_id, name, status) — the fields the panel reads."""
    page.evaluate(
        """(specs) => {
          for (const [job_id, name, status] of specs) {
            __winnow.ingestJobs.push({ job_id, name, status, kind: 'ingest', result: [{ row_count: 10 }] });
          }
          __winnow.renderJobsPanel();
        }""",
        list(specs),
    )


def _names(page):
    return page.locator(f"{ROWS} .job-name").all_text_contents()


def test_clear_all_takes_the_whole_pile_and_hides_the_panel(page):
    _jobs(page, *[(900 + i, f"file{i}.csv", "done") for i in range(12)])
    assert len(_names(page)) == 12
    page.locator(CLEAR).click()
    page.wait_for_selector("#jobsPanel[hidden]", state="attached")


def test_failed_and_cancelled_rows_go_too(page):
    """The ones that never auto-dismiss, which is why the pile survives a
    folder of files Winnow could not read."""
    _jobs(page, (901, "ok.csv", "done"), (902, "bad.bin", "error"), (903, "stopped.csv", "cancelled"))
    assert len(_names(page)) == 3
    page.locator(CLEAR).click()
    page.wait_for_selector("#jobsPanel[hidden]", state="attached")


def test_running_and_queued_work_is_not_cleared(page):
    _jobs(page,
          (901, "done.csv", "done"),
          (902, "importing.csv", "running"),
          (903, "waiting.csv", "queued"))
    page.locator(CLEAR).click()
    page.wait_for_function("() => document.querySelectorAll('#jobsPanel .job-row').length === 2")
    # The running row by name, the queued one rolled into its summary row.
    assert "importing.csv" in " ".join(_names(page))
    assert "1 queued" in " ".join(_names(page))
    # Still there, still running — the button dismissed nothing it owns.
    assert page.evaluate("() => __winnow.ingestJobs.filter((j) => j.status === 'running').length") == 1


def test_the_header_counts_what_it_will_clear_not_what_is_on_screen(page):
    """Eleven rows, eight of them clearable: the running import and the
    queued summary are not what the button acts on. A header reading
    "11 finished" over a panel that loses eight rows would be the control
    explaining itself wrongly, which is worse than no count at all."""
    _jobs(page,
          (901, "importing.csv", "running"),
          (902, "waiting-a.csv", "queued"),
          (903, "waiting-b.csv", "queued"),
          *[(910 + i, f"landed{i}.csv", "done") for i in range(5)],
          *[(920 + i, f"unreadable{i}.bin", "error") for i in range(3)])
    assert page.locator(COUNT).inner_text().lower() == "8 finished"
    before = page.locator(ROWS).count()
    page.locator(CLEAR).click()
    page.wait_for_function("() => document.querySelectorAll('#jobsPanel .job-row').length === 2")
    # The 8 that went are the 8 it counted; what is left is the running row
    # and the queued summary, which is 2 rows standing for 3 jobs.
    assert before - 2 == 8


def test_the_button_is_absent_when_there_is_nothing_to_clear(page):
    """Present and absent in the one test. "Absent" on its own is a claim
    any build that never draws the button at all satisfies, including the
    one before this feature — so the clearable state has to be shown
    first, in the same panel, for the empty state to mean anything."""
    _jobs(page, (901, "done.csv", "done"))
    assert page.locator(CLEAR).count() == 1
    page.evaluate("""() => {
      __winnow.ingestJobs.length = 0;
      __winnow.ingestJobs.push({ job_id: 902, name: 'importing.csv', status: 'running', kind: 'ingest', result: [] });
      __winnow.renderJobsPanel();
    }""")
    assert page.locator(ROWS).count() == 1
    assert page.locator(CLEAR).count() == 0


def test_a_notice_that_holds_a_result_survives_and_says_so(page):
    """The background-search shape: landed, holding a built view
    server-side, and waiting for Apply or Discard. Clear all is not an
    answer to that question, so the row stays and the toast explains the
    survivor.

    Built with `holdsResult` rather than with buttons, because buttons
    are what this used to key off and buttons are the wrong signal —
    see test_a_watchlist_style_alert_is_cleared below."""
    _jobs(page, (901, "done.csv", "done"))
    page.evaluate("""() => {
      window.__dismissed = false;
      window.__n = __winnow.createNotice('view', { title: 'Searching "evil" in Security.csv' },
        { onDismiss: () => { window.__dismissed = true; }, holdsResult: true });
      __n.done({ detail: '412 rows', sticky: true, actions: [
        { label: 'Apply', onClick: () => {} },
        { label: 'Discard', onClick: () => {} },
      ] });
    }""")
    page.wait_for_selector(f"{ROWS} .job-action")
    page.locator(CLEAR).click()
    page.wait_for_function("() => document.querySelectorAll('#jobsPanel .job-row').length === 1")
    assert 'Searching "evil" in Security.csv' in " ".join(_names(page))
    assert page.evaluate("() => window.__dismissed") is False       # not answered behind their back
    assert "waiting for an answer" in page.locator("#toast").inner_text()


def test_a_watchlist_style_alert_is_cleared(page, fake_plugin_mount):
    """The row the feature exists for, and the one the first rule got
    wrong. watchlist.js announceHits settles its scan row as done +
    sticky + an "Open watchlist" button, and a folder import is what
    PRODUCES it — every landed import runs scanWatchlistForSources. It
    carries a button but holds nothing: the button is a shortcut to a
    tab, the hits are already written server-side, and sticky-with-
    buttons also skips the linger timer, so nothing else ever takes the
    row away. Keying on buttons meant Clear all refused exactly this row
    and told the analyst it was waiting for an answer."""
    page.evaluate("""() => {
      window.__n = __ctx.notify({ title: 'Watchlist scan' });
      __n.done({ title: 'Watchlist: 47 hits', detail: '47 in Security.csv',
                 sticky: true, actions: [{ label: 'Open watchlist', onClick: () => {} }] });
    }""")
    page.wait_for_selector(f"{ROWS} .job-action")
    page.locator(CLEAR).click()
    page.wait_for_selector("#jobsPanel[hidden]", state="attached")
    assert page.locator("#toast").inner_text().strip() == "" or \
        "waiting for an answer" not in page.locator("#toast").inner_text()


def test_a_running_notice_is_never_cleared(page, fake_plugin_mount):
    """The refusal that matters most, and the one no other test covers.
    A background search still running owns a notice whose onDismiss
    cancels the search server-side; a watchlist scan's cancels the scan.
    Clearing those would turn "tidy up my notifications" into "stop what
    I asked for". Ingest jobs are guarded by a different predicate
    (clearableJob), so without this the `status !== 'running'` half of
    clearableNotice has nothing holding it in place."""
    _jobs(page, (901, "done.csv", "done"))
    page.evaluate("""() => {
      window.__cancelled = false;
      window.__n = __winnow.createNotice('view', { title: 'Searching in the background', progress: null },
        { onDismiss: () => { window.__cancelled = true; } });
    }""")
    page.wait_for_function(f"() => document.querySelectorAll('{ROWS}').length === 2")
    page.locator(CLEAR).click()
    page.wait_for_function("() => document.querySelectorAll('#jobsPanel .job-row').length === 1")
    assert "Searching in the background" in " ".join(_names(page))
    assert page.evaluate("() => window.__cancelled") is False       # and never cancelled


def test_a_finished_notice_without_buttons_goes(page):
    page.evaluate("""() => {
      window.__dismissed = false;
      window.__n = __winnow.createNotice('test', { title: 'scan', sticky: true },
        { onDismiss: () => { window.__dismissed = true; } });
      __n.done({ detail: 'finished' });
    }""")
    page.wait_for_selector(f"{ROWS}.job-notice")
    page.locator(CLEAR).click()
    page.wait_for_selector("#jobsPanel[hidden]", state="attached")
    # The row goes, its onDismiss does NOT run. That handler means "the ✕
    # must act on this rather than merely hide it", which is only true of
    # a row standing for something live — and those are the rows Clear all
    # keeps. Firing it here would aim past the row: the detached search's
    # calls off that table's background search, and a receipt for a search
    # that failed long ago is no instruction about the one running now.
    # (view.js scopes that call to its own record, so it would no-op; the
    # rule stands on its own, for handlers nobody has written yet.)
    assert page.evaluate("() => window.__dismissed") is False


def test_an_error_notice_goes_even_with_a_button_left_on_it(page, fake_plugin_mount):
    """`applyNoticeOpts` leaves what it is not given, so a notice created
    with a Cancel keeps that Cancel through `fail()` unless the caller
    clears it. A failure is never asking a question, so the rule reads the
    status and not just the buttons."""
    page.evaluate("""() => {
      window.__n = __ctx.notify({ title: 'broken', actions: [{ label: 'Cancel', onClick: () => {} }] });
      __n.fail({ detail: 'boom' });
    }""")
    page.wait_for_selector(f"{ROWS}.job-notice")
    assert page.locator(f"{ROWS} .job-action").count() == 1
    page.locator(CLEAR).click()
    page.wait_for_selector("#jobsPanel[hidden]", state="attached")


def test_the_button_stays_reachable_under_a_long_pile(page):
    """Sticky, because the panel caps at 50vh and scrolls. A Clear all
    that scrolled off the top with the rows would be exactly as much work
    as the ✕s it replaces.

    Twenty rows, which is `Store.INGEST_JOB_KEEP` — the most finished
    imports the server will ever hand back, so this is the tallest pile
    that can really happen rather than an invented one."""
    _jobs(page, *[(900 + i, f"file{i}.csv", "done") for i in range(20)])
    panel = page.locator(PANEL)
    assert panel.evaluate("(e) => e.scrollHeight > e.clientHeight"), "expected the panel to overflow"
    panel.evaluate("(e) => { e.scrollTop = e.scrollHeight; }")
    page.wait_for_function("() => document.querySelector('#jobsPanel').scrollTop > 0")
    # Still inside the panel's own box after scrolling to the bottom.
    assert page.evaluate("""() => {
      const p = document.querySelector('#jobsPanel').getBoundingClientRect();
      const b = document.querySelector('#jobsPanel .jobs-clear').getBoundingClientRect();
      return b.top >= p.top - 1 && b.bottom <= p.bottom + 1;
    }""")
    page.locator(CLEAR).click()
    page.wait_for_selector("#jobsPanel[hidden]", state="attached")
