"""Across cases: the IOC sweep, the cross-case query and the unified
timeline, driven through the modal against real secondary case files.

Those cases are created here and registered, so the test does not depend
on what the shared session case happens to hold — and they are
unregistered again afterwards.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

ROOT = Path(__file__).resolve().parent.parent.parent
HEADER = ["Timestamp", "EventId", "Host", "User"]


@pytest.fixture
def other_cases(page, tmp_path):
    """Two case files on disk, registered so the routes will read them."""
    sys.path.insert(0, str(ROOT))
    from winnow.store import DEFAULT_TAGS, Store

    made = []
    for host, rows in (("mcHostA", [["2024-01-05 13:22:01", "4624", "HOST-A", "alice"],
                                    ["2024-01-05 14:00:00", "4625", "HOST-A", "evil.exe"]]),
                       ("mcHostB", [["2024-01-05 13:30:00", "4624", "HOST-B", "evil.exe"]])):
        c = tmp_path / f"{host}.csv"
        with open(c, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(HEADER)
            w.writerows(rows)
        db = tmp_path / f"{host}.db"
        st = Store(str(db), default_tags=DEFAULT_TAGS)
        st.ingest_csv(str(c), name=f"{host}.csv", build_fts=False)
        st.close()
        made.append(str(db))

    # Register them in the SERVER's workspace, through its own API.
    for p, name in zip(made, ("mcHostA", "mcHostB")):
        page.evaluate(
            """([path, name]) => fetch('/api/cases', { method: 'POST',
                 headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
                 body: JSON.stringify({ path, name, group: 'mc-test', notes: '' }) })""",
            [p, name])
    yield made
    page.evaluate(
        """async () => {
             const h = { 'X-Timeline-Lite-Client': '1' };
             const cases = await fetch('/api/cases', { headers: h }).then((r) => r.json());
             for (const c of cases) if (c.group === 'mc-test')
               await fetch('/api/cases/' + c.id, { method: 'DELETE', headers: h });
           }""")


MODES = {"IOC sweep": "sweep", "Query": "sql", "Timeline": "timeline"}


def _pick_only_test_cases(page):
    """Select just this test's cases. The full suite registers others, and
    a query attaches at most 8 — without this, a run with many cases
    registered fails on the budget before reaching what is under test."""
    picked = page.evaluate("""() => {
         let n = 0;
         for (const cb of document.querySelectorAll('#modalBody .mc-case input')) {
           const want = cb.parentElement.textContent.includes('mcHost');
           if (cb.checked !== want) cb.click();
           if (want) n++;
         }
         return n;
       }""")
    assert picked == 2, f"expected this test's two cases to be selectable, got {picked}"


def _open(page, tab):
    page.evaluate("() => __winnow.openMultiCase()")
    # .mc-cases is appended BEFORE the case list is fetched, so waiting on
    # the container races the rows. Wait for this test's own cases, which
    # only exist once the list has painted.
    page.wait_for_selector("#modal:not([hidden]) .mc-case")
    page.wait_for_function(
        """() => [...document.querySelectorAll('#modalBody .mc-case')]
             .filter((r) => r.textContent.includes('mcHost')).length === 2""",
        timeout=15_000)
    page.locator("#modalBody .mc-tabs .btn", has_text=tab).click()
    # The panel carries which mode it is, so this waits for THIS tab's
    # controls rather than whichever ones happened to be there already.
    page.wait_for_selector(f"#modalBody .mc-controls[data-mode={MODES[tab]}]")


def test_the_sweep_finds_an_ioc_in_the_other_cases(page, other_cases):
    try:
        _open(page, "IOC sweep")
        page.locator("#modalBody .mc-values").fill("evil.exe")
        page.locator("#modalBody .mc-controls .btn", has_text="Sweep").click()
        page.wait_for_selector("#modalBody .mc-hit")
        text = page.locator("#modalBody .mc-out").inner_text()
        assert "2 hits" in text
        assert "mcHostA" in text and "mcHostB" in text
        # each hit offers the way back into the case that owns it
        assert page.locator("#modalBody .mc-hit .mc-open").count() == 2
    finally:
        page.keyboard.press("Escape")


def test_a_query_runs_across_two_cases(page, other_cases):
    try:
        _open(page, "Query")
        _pick_only_test_cases(page)      # so c1/c2 are predictable
        page.locator("#modalBody .mc-sql").fill(
            "SELECT 'A' AS c, COUNT(*) FROM c1.src_1 UNION ALL SELECT 'B', COUNT(*) FROM c2.src_1")
        page.locator("#modalBody .mc-controls .btn", has_text="Run").click()
        page.wait_for_selector("#modalBody .mc-table")
        text = page.locator("#modalBody .mc-out").inner_text()
        assert "c1 = mcHostA" in text and "c2 = mcHostB" in text
        rows = page.locator("#modalBody .mc-table tr").count()
        assert rows == 3, "a header and two rows"
    finally:
        page.keyboard.press("Escape")


def test_a_forbidden_statement_is_refused_in_the_ui(page, other_cases):
    try:
        _open(page, "Query")
        _pick_only_test_cases(page)
        page.locator("#modalBody .mc-sql").fill("PRAGMA table_info(sources)")
        page.locator("#modalBody .mc-controls .btn", has_text="Run").click()
        # Wait for the TEXT: the "Running…" placeholder is a .note-status
        # too, so waiting for the element matched it and the assertion then
        # raced the response.
        page.wait_for_function(
            """() => (document.querySelector('#modalBody .mc-out') || {}).textContent
                     ?.includes("aren't allowed")""",
            timeout=10_000)
    finally:
        page.keyboard.press("Escape")


def test_the_schema_names_the_aliases(page, other_cases):
    try:
        _open(page, "Query")
        _pick_only_test_cases(page)
        page.locator("#modalBody .mc-controls .btn", has_text="Show schema").click()
        page.wait_for_selector("#modalBody .mc-schema")
        assert "attached as c1" in page.locator("#modalBody .mc-schema").inner_text()
    finally:
        page.keyboard.press("Escape")


def test_the_timeline_interleaves_the_cases(page, other_cases):
    try:
        _open(page, "Timeline")
        page.locator("#modalBody .mc-controls .btn", has_text="Build").click()
        page.wait_for_selector("#modalBody .mc-hit")
        whens = page.locator("#modalBody .mc-hit .mc-when").all_inner_texts()
        assert whens == sorted(whens), "rows must be in time order"
        text = page.locator("#modalBody .mc-out").inner_text()
        assert "mcHostA" in text and "mcHostB" in text
    finally:
        page.keyboard.press("Escape")


def test_the_query_panel_warns_before_the_attach_limit(page, other_cases):
    """With more cases selected than SQLite can attach, say so up front
    rather than letting the run fail."""
    try:
        _open(page, "Query")
        over = page.evaluate("""() => {
             const boxes = [...document.querySelectorAll('#modalBody .mc-case input')];
             for (const cb of boxes) if (!cb.checked && !cb.disabled) cb.click();
             return boxes.filter((b) => b.checked).length;
           }""")
        page.locator("#modalBody .mc-tabs .btn", has_text="IOC sweep").click()
        page.locator("#modalBody .mc-tabs .btn", has_text="Query").click()
        note = page.locator("#modalBody .mc-controls .fb-help").first.inner_text()
        if over > 8:
            assert "at most 8" in note and "Deselect" in note
        else:
            assert "Up to 8 cases" in note
    finally:
        page.keyboard.press("Escape")
