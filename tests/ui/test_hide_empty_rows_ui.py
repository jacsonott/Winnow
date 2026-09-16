"""The table menu's "Hide empty rows" toggle in the browser: the view
shrinks and the toolbar count follows, the toggle survives a tab switch
and a reload (it lives in the layout), and it is off for other tables.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture
def gappy(page, server_post, ui_csv):
    p = Path(ui_csv).parent / "gappy.csv"
    p.write_text("A,B\n1,x\n,\n2,\n,\n3,z\n")
    server_post("/api/ingest/jobs/path", {"path": str(p), "build_fts": False})
    # Queued from outside the page, so nothing in it polls the job: refresh
    # the source list ourselves until the table is there.
    for _ in range(100):
        page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
        if page.evaluate("() => __winnow.S.sources.some((s) => s.name === 'gappy.csv' && s.row_count === 5)"):
            break
        page.wait_for_timeout(200)
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'gappy.csv').id")
    yield sid
    page.evaluate("(id) => __winnow.api(`/api/sources/${id}`, { method: 'DELETE' }).catch(() => {})", sid)


def _open(page, sid):
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view", arg=sid)


def test_toggle_hides_rows_and_is_remembered(page, gappy):
    _open(page, gappy)
    assert page.evaluate("() => __winnow.S.view.row_count") == 5
    page.evaluate("() => __winnow.openTableMenu()")
    btn = page.locator("#modalBody button", has_text="Hide empty rows")
    btn.wait_for(state="visible")
    assert btn.get_attribute("aria-pressed") == "false"
    btn.click()
    page.wait_for_function("() => __winnow.S.hideEmptyRows === true && __winnow.S.view.row_count === 3")
    assert btn.get_attribute("aria-pressed") == "true"
    page.evaluate("() => __winnow.closeModal()")
    assert "3" in page.locator("#viewStats b").inner_text()
    # Off for another table; back on when we return
    other = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'ui.csv').id")
    _open(page, other)
    assert page.evaluate("() => __winnow.S.hideEmptyRows") is False
    _open(page, gappy)
    page.wait_for_function("() => __winnow.S.hideEmptyRows === true && __winnow.S.view.row_count === 3")
    # Survives a reload: the layout carries it
    page.wait_for_timeout(600)   # saveLayout is debounced 400 ms
    page.reload(wait_until="networkidle")
    page.wait_for_selector(".row")
    _open(page, gappy)
    page.wait_for_function("() => __winnow.S.hideEmptyRows === true && __winnow.S.view.row_count === 3")
    # And off again
    page.evaluate("() => __winnow.openTableMenu()")
    page.locator("#modalBody button", has_text="Hide empty rows").click()
    page.wait_for_function("() => __winnow.S.hideEmptyRows === false && __winnow.S.view.row_count === 5")
    page.evaluate("() => __winnow.closeModal()")
