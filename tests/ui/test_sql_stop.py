"""A runaway SQL pane query can be stopped from the page.

The pane runs whatever was typed, and some of it never ends — a recursive
CTE with no stop condition is one typo away. While one ran the pane said
"Running…" for ever and offered no way out: the busy chip every other long
request gets was never armed for a query, so there was nothing to click.
The chip is the query's Stop button now, and what it stops is the query on
the server, not just the waiting (tests/test_sql_runaway.py has the server
half — a closed window and Shut down stop it too)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# Bounded so a regression costs minutes of the shared server's CPU rather
# than all of it for the rest of the session.
RUNAWAY = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
           "WHERE x < 1000000000) SELECT count(*) FROM c")


def test_a_runaway_query_is_stopped_from_the_busy_chip(page, open_sql):
    open_sql(page)
    editor = page.locator("#sqlText")
    was = editor.input_value()
    try:
        editor.fill(RUNAWAY)
        page.click("#btnRunSql")
        # Armed once the query has run long enough to be worth stopping.
        page.wait_for_selector("#busyCancel:not([hidden])", timeout=10_000)
        page.click("#busyCancel")

        # The answer comes back from the SERVER — a 499 exists only once the
        # query has actually been interrupted there.
        page.wait_for_selector("#sqlResult .sql-error", timeout=15_000)
        assert page.locator("#sqlResult .sql-error").inner_text() == "Query cancelled"
        page.wait_for_selector("#busyCancel", state="hidden")

        # And the pane is usable straight away.
        editor.fill("SELECT 1 AS one")
        page.click("#btnRunSql")
        page.wait_for_selector("#sqlResult table")
    finally:
        # The editor's text is saved to the shared case's SQL tab.
        editor.fill(was)
