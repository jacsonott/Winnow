"""Across cases, back in the header (static/js/across.js): the sweep, the
findings Timeline and the query answer in ONE shape — Timeline lines that
carry their case — and a line leads somewhere: tag it in the case that
owns it, or open it there. Driven against case files made and registered
here, and unregistered again afterwards; the shared session case is left
out of every answer by choosing only this test's cases.

The old modal (multicase.js) stays reachable as __winnow.openMultiCase()
until it is removed, so that is pinned too.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

ROOT = Path(__file__).resolve().parent.parent.parent
HEADER = ["Timestamp", "EventId", "Host", "User"]
HOSTS = (("acHostA", [["2024-01-05 13:22:01", "4624", "HOST-A", "alice"],
                      ["2024-01-05 14:00:00", "4625", "HOST-A", "acEvil.exe"]]),
         ("acHostB", [["2024-01-05 13:30:00", "4624", "HOST-B", "acEvil.exe"]]))


@pytest.fixture
def other_cases(page, tmp_path):
    """Two case files on disk, registered through the server's own API."""
    sys.path.insert(0, str(ROOT))
    from winnow.store import DEFAULT_TAGS, Store

    made = []
    for host, rows in HOSTS:
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
    for p, (name, _rows) in zip(made, HOSTS):
        page.evaluate(
            """([path, name]) => fetch('/api/cases', { method: 'POST',
                 headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
                 body: JSON.stringify({ path, name, group: 'ac-test', notes: '' }) })""",
            [p, name])
    yield made
    page.evaluate(
        """async () => {
             const h = { 'X-Timeline-Lite-Client': '1' };
             const cases = await fetch('/api/cases', { headers: h }).then((r) => r.json());
             for (const c of cases) if (c.group === 'ac-test')
               await fetch('/api/cases/' + c.id, { method: 'DELETE', headers: h });
           }""")


def _open(page, paths, section):
    page.locator("#btnAcrossCases").click()
    page.wait_for_selector("#modal:not([hidden]) .ac-panes")
    # Only this test's cases: the shared case and whatever other modules
    # registered are not part of the question.
    page.evaluate("(paths) => { __winnow.AC.picked = new Set(paths); }", paths)
    page.locator(f'#modalBody .side-rail-item[data-section="{section}"]').click()
    page.wait_for_selector(f'#modalBody .side-pane[data-section="{section}"]')


def test_the_button_is_back_and_the_old_modal_still_answers(page):
    assert page.locator("#btnAcrossCases").is_visible()
    assert page.evaluate("() => typeof __winnow.openMultiCase") == "function"


def test_a_sweep_answers_in_lines_with_their_case(page, other_cases):
    try:
        _open(page, other_cases, "sweep")
        page.locator("#modalBody .ac-values").fill("acEvil.exe")
        page.locator("#modalBody .side-pane .btn.primary").click()
        page.wait_for_function(
            "() => /Read 2 of 2 cases/.test((document.querySelector('#modalBody .ac-status') || {}).textContent || '')",
            timeout=20_000)
        lines = page.locator("#modalBody .ac-line")
        assert lines.count() == 2
        cases = page.locator("#modalBody .ac-line .ac-case-pill").all_inner_texts()
        assert sorted(cases) == ["acHostA", "acHostB"]
        assert page.locator("#modalBody .ac-line .ac-matched").all_inner_texts() == ["acEvil.exe"] * 2
        assert "2 hits in 2 cases" in page.locator("#modalBody .ac-status").inner_text()
    finally:
        page.keyboard.press("Escape")


def test_a_hit_is_tagged_in_the_case_that_owns_it(page, other_cases):
    path = other_cases[1]
    try:
        _open(page, other_cases, "sweep")
        page.locator("#modalBody .ac-values").fill("acEvil.exe")
        page.locator("#modalBody .side-pane .btn.primary").click()
        line = page.locator("#modalBody .ac-line", has=page.locator(".ac-case-pill", has_text="acHostB"))
        line.wait_for()
        line.click()
        chip = page.locator("#modalBody .ac-detail .ac-tag").first
        chip.wait_for()
        name = chip.inner_text()
        chip.click()
        page.wait_for_selector('#modalBody .ac-detail .ac-tag[aria-pressed="true"]')
        # Written into acHostB's own file, not the open case.
        row = page.evaluate(
            """(path) => fetch('/api/across/row?path=' + encodeURIComponent(path) + '&source_id=1&rid=1',
                 { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""", path)
        assert [t["name"] for t in row["tags"]] == [name]
        # ...and it is a finding now, on the cross-case Timeline.
        page.locator('#modalBody .side-rail-item[data-section="timeline"]').click()
        page.locator("#modalBody .side-pane .btn.primary").click()
        page.wait_for_selector("#modalBody .ac-story .ac-line")
        assert page.locator("#modalBody .ac-story .ac-line .ac-case-pill").all_inner_texts() == ["acHostB"]
    finally:
        page.keyboard.press("Escape")


def test_a_query_that_names_its_rows_comes_back_as_lines(page, other_cases):
    try:
        _open(page, other_cases, "query")
        page.locator("#modalBody .ac-sql").fill("SELECT rid, User FROM c1.src_1 WHERE User = 'acEvil.exe'")
        page.locator("#modalBody .side-pane .btn.primary").click()
        page.wait_for_selector("#modalBody .ac-line")
        assert page.locator("#modalBody .ac-line .ac-case-pill").all_inner_texts() == ["acHostA"]
        page.locator("#modalBody .side-pane .btn", has_text="As a table").click()
        page.wait_for_selector("#modalBody .mc-table")
        assert page.locator("#modalBody .mc-table tr").count() == 2
    finally:
        page.keyboard.press("Escape")
