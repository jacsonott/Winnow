"""Importing a batch of files must not drag the analyst around.

Reported from a real import: with many files queued, every completion
called loadSources(), which re-opened the source it had auto-opened for
the FIRST file — switching back to the grid from wherever the analyst had
navigated, and resetting that table's filters and search on the way.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture(autouse=True)
def clean_up_imports(page):
    """The UI server's case is shared with every other module, so a test
    that imports files has to take them away again — sources, open tabs and
    the frontend bookkeeping the real Remove flow does. Leaving them behind
    surfaces as a failure in an unrelated module (a merge builder listing
    tables it did not expect), far from the cause."""
    before = set(page.evaluate("() => __winnow.S.sources.map((s) => s.id)"))
    yield
    page.evaluate(
        """async (before) => {
             const S = __winnow.S;
             // Let any in-flight grid fetch settle before the source goes.
             await new Promise((r) => setTimeout(r, 150));
             const mine = S.sources.filter((s) => !before.includes(s.id));
             for (const s of mine) {
               S.viewCache?.delete?.(s.id);
               await __winnow.api('/api/source/' + s.id, { method: 'DELETE' }).catch(() => {});
             }
             S.sourceId = null;
             S.view = null;
             await __winnow.loadSources();
           }""",
        sorted(before))


def _import(page, path, name):
    """The post-then-poll the import modal does."""
    page.evaluate(
        """([path, name]) => __winnow.post('/api/ingest/jobs/path',
             { path, name, kind: 'csv' }).then(() => __winnow.startJobsPoll())""",
        [str(path), name])


def _csv(tmp_path, name, rows=3):
    p = tmp_path / name
    p.write_text("Host,User\n" + "".join(f"h{i},u{i}\n" for i in range(rows)))
    return p


def test_a_finished_import_leaves_a_page_tab_alone(page, tmp_path):
    """The analyst is reading the SQL pane while files land."""
    before = page.evaluate("() => __winnow.S.sources.length")
    page.evaluate("() => __winnow.showSqlTab()")
    page.wait_for_function("() => __winnow.S.activeTab === 'sql'")

    _import(page, _csv(tmp_path, "landing.csv"), "landing.csv")
    page.wait_for_function("(n) => __winnow.S.sources.length > n", arg=before, timeout=20_000)
    page.wait_for_timeout(400)          # let the poll's refresh land

    assert page.evaluate("() => __winnow.S.activeTab") == "sql", \
        "a finished import switched away from the SQL pane"
    assert page.locator("#sqlview").is_visible()
    # …and the new table did show up in the sidebar without navigating there
    assert page.locator("#sidebarList").inner_text().count("landing.csv") >= 1
    page.evaluate("() => __winnow.showGridTab()")


def test_a_finished_import_does_not_rebuild_the_grid_underneath_you(page, tmp_path):
    """On a table: re-opening the source tore the rendered rows down and
    refetched them. Once per completed file, that is the "locks up" in the
    report — so pin that the rows on screen are the same DOM nodes."""
    first = page.evaluate("() => __winnow.S.sourceId")
    assert first is not None
    page.wait_for_selector("#body .row")
    page.evaluate("() => { document.querySelector('#body .row').dataset.sentinel = 'kept'; }")

    _import(page, _csv(tmp_path, "second.csv"), "second.csv")
    # The completion toast is emitted in the same block that then refreshes,
    # so waiting for it (rather than a bare timeout) makes this deterministic.
    page.wait_for_function(
        """() => { const t = document.getElementById('toast');
                   return t && !t.hidden && t.textContent.includes('rows imported'); }""",
        timeout=20_000)
    page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'second.csv')",
                           timeout=20_000)
    page.wait_for_timeout(600)

    assert page.evaluate("() => __winnow.S.sourceId") == first, "the import moved the grid"
    assert page.evaluate("() => __winnow.S.activeTab") == "grid"
    assert page.evaluate(
        "() => (document.querySelector('#body .row') || {}).dataset?.sentinel") == "kept", \
        "the grid was torn down and re-rendered by a background import"


def test_several_imports_in_a_row_never_move_the_analyst(page, tmp_path):
    """The reported shape: a batch, arriving one at a time."""
    page.evaluate("() => __winnow.showSqlTab()")
    page.wait_for_function("() => __winnow.S.activeTab === 'sql'")
    before = page.evaluate("() => __winnow.S.sources.length")
    for i in range(3):
        _import(page, _csv(tmp_path, f"batch{i}.csv"), f"batch{i}.csv")
    page.wait_for_function("(n) => __winnow.S.sources.length >= n + 3", arg=before, timeout=30_000)
    page.wait_for_timeout(600)
    assert page.evaluate("() => __winnow.S.activeTab") == "sql"
    page.evaluate("() => __winnow.showGridTab()")


def test_an_explicit_selection_still_navigates(page, tmp_path):
    """navigate:false is only for background refreshes — asking for a
    source by id must still open it."""
    before = page.evaluate("() => __winnow.S.sources.length")
    _import(page, _csv(tmp_path, "explicit.csv"), "explicit.csv")
    page.wait_for_function("(n) => __winnow.S.sources.length > n", arg=before, timeout=20_000)
    target = page.evaluate(
        "() => (__winnow.S.sources.find((s) => s.name === 'explicit.csv') || {}).id")
    assert target is not None

    page.evaluate("() => __winnow.showSqlTab()")
    page.wait_for_function("() => __winnow.S.activeTab === 'sql'")
    page.evaluate("(id) => __winnow.loadSources(id, { navigate: false })", target)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.activeTab === 'grid'",
                           arg=target, timeout=10_000)


def test_the_first_import_into_an_empty_view_still_opens(page, tmp_path):
    """The behaviour worth keeping: with nothing on screen, a finished
    import lands on its table rather than leaving an empty pane."""
    page.evaluate("() => { __winnow.S.sourceId = null; __winnow.S.view = null; }")
    before = page.evaluate("() => __winnow.S.sources.length")
    _import(page, _csv(tmp_path, "opens.csv"), "opens.csv")
    page.wait_for_function("(n) => __winnow.S.sources.length > n", arg=before, timeout=20_000)
    page.wait_for_function("() => __winnow.S.sourceId !== null", timeout=10_000)
    assert page.evaluate("() => __winnow.S.activeTab") == "grid"
