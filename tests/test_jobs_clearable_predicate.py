"""One spelling of "is this a jobs row Clear all can take".

`clearableJob` in static/js/jobs.js answers that question for two callers
that must agree: `clearableCount`, which is the number in the panel
header ("8 finished"), and `clearFinishedNotices`, which is the set of
rows the button actually dismisses. `renderJobsPanel` draws that same set
as the finished rows, and for a while it did so by writing the rule out a
second time instead of calling the predicate.

Two copies of one rule are a header that quietly stops describing the
button beneath it. Teach `clearableJob` about a status the copy has never
heard of and the count starts including rows the panel does not draw, or
the panel draws rows the button refuses — which is exactly the miscount
the clearable count was introduced to prevent, arriving by a different
door.

This has to be asserted against the source. No browser test can see it:
while the two copies agree they are indistinguishable from one shared
predicate, and by the time they disagree the panel is already wrong.
"""

from __future__ import annotations

from pathlib import Path

JOBS_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "jobs.js"

# Whitespace-normalised before searching, so a copy that happens to be
# wrapped across two lines — which is how the duplicate was written — is
# still found rather than silently passing.
RULE = "j.status !== 'running' && j.status !== 'queued'"


def _source() -> str:
    return " ".join(JOBS_JS.read_text(encoding="utf-8").split())


def _render_jobs_panel() -> str:
    """Just that one function's body. The whole-file search below cannot
    stand in for this: `clearableCount` filters `ingestJobs` with the
    predicate too, so a file-wide match would be satisfied by a caller
    that is not the one under test."""
    src = JOBS_JS.read_text(encoding="utf-8")
    start = src.index("export function renderJobsPanel() {")
    end = src.index("\n}\n", start)
    return " ".join(src[start:end].split())


def test_the_clearable_rule_is_written_once():
    src = _source()
    assert RULE in src, (
        "clearableJob's rule is no longer spelled like this — if it moved or "
        "changed shape, move this test with it rather than deleting it"
    )
    assert src.count(RULE) == 1, (
        f"the clearable rule appears {src.count(RULE)} times in jobs.js. It belongs "
        "to clearableJob alone; a second copy is how the header's count and the "
        "rows Clear all removes come to disagree"
    )


def test_the_panel_draws_its_finished_rows_through_the_predicate():
    """The other half of the same invariant: having one copy of the rule
    is no help if renderJobsPanel stopped asking it."""
    body = _render_jobs_panel()
    assert "ingestJobs.filter(clearableJob)" in body, (
        "renderJobsPanel's finished rows must come from clearableJob, so they are "
        "the rows the header counted"
    )
    assert RULE not in body, (
        "renderJobsPanel is testing the clearable rule itself again instead of "
        "calling clearableJob"
    )
