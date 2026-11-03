"""Dropping files on the home screen: on a case's row they go into THAT
case, anywhere else on the home screen they start a fresh quick look. Both
open the case and hand the files to the import dialog, so the analyst still
reviews the settings before anything is read. Neither drop had ever run in
a test: the in-app drop is a different handler (the window's overlay).

The two targets are nested (a row is inside the home screen), and the row
stops its drop from reaching the home screen's handler. Without that, a
drop on a case would also make a quick look and open it over the case the
analyst aimed at.

Each test has its own server: both switch the server's case, and the
shared server's case is every module's.
"""

from __future__ import annotations

import pytest

from spawn import running_server

pytestmark = pytest.mark.ui

# A drag of files the way Chromium delivers one from the desktop: the File
# objects in a DataTransfer whose types include "Files". `kind` "text" is a
# drag of a text selection instead, which neither handler is for.
DRAG = """([selector, type, kind]) => {
  const dt = new DataTransfer();
  if (kind === 'text') dt.setData('text/plain', 'not a file');
  else dt.items.add(new File(['Host,EventId\\nWKS1,4624\\n'], 'dropped.csv', { type: 'text/csv' }));
  const ev = new DragEvent(type, { dataTransfer: dt, bubbles: true, cancelable: true });
  document.querySelector(selector).dispatchEvent(ev);
  return ev.defaultPrevented;
}"""

ROW = "#home .home-row"


def _drop(pg, selector, kind="files"):
    over = pg.evaluate(DRAG, [selector, "dragover", kind])
    pg.evaluate(DRAG, [selector, "drop", kind])
    return over


def _queued(pg):
    return pg.evaluate("() => __winnow.S.importQueue.map((i) => i.name)")


@pytest.fixture
def home_with_a_case(own_page, tmp_path):
    with running_server(tmp_path, "--no-fts") as srv:
        target = str(tmp_path / "cases" / "target.db-winnow")
        srv.post("/api/cases", {"path": target, "name": "Drop target", "group": "", "notes": ""})
        # A case on the list already, so no first-run folder prompt.
        with own_page(base=srv.base + "/", viewport=(1200, 800), wait_until="load",
                      ready="#home:not([hidden]) .home-row", ready_timeout=20_000) as pg:
            yield srv, pg, target


def test_files_dropped_on_a_case_go_into_that_case(home_with_a_case):
    srv, pg, target = home_with_a_case
    # The row lights up, and the home screen behind it does not.
    over = pg.evaluate(DRAG, [ROW, "dragover", "files"])
    assert over, "the row did not accept the drag"
    assert pg.evaluate("(s) => document.querySelector(s).classList.contains('home-case-drop')", ROW)
    assert not pg.evaluate("() => document.getElementById('home').classList.contains('home-drop')")

    pg.evaluate(DRAG, [ROW, "drop", "files"])
    pg.wait_for_selector("#app:not([hidden])", timeout=15_000)
    pg.wait_for_function("() => document.getElementById('brandLabel').textContent === 'Drop target'")
    pg.wait_for_function("() => /import/i.test(document.getElementById('modalTitle').textContent)")
    assert _queued(pg) == ["dropped.csv"]
    cur = srv.get("/api/case/current")
    assert cur["path"] == target and not cur["temp"]
    # The drop went to the row only: no quick look was made beside it.
    assert [c["name"] for c in srv.get("/api/cases")] == ["Drop target"]
    assert pg.locator("#tempBanner").is_hidden()


def test_files_dropped_off_the_case_rows_start_a_quick_look(home_with_a_case):
    srv, pg, target = home_with_a_case
    assert _drop(pg, "#home .home-foot"), "the home screen did not accept the drag"
    pg.wait_for_selector("#tempBanner:not([hidden])", timeout=15_000)
    pg.wait_for_function("() => /import/i.test(document.getElementById('modalTitle').textContent)")
    assert _queued(pg) == ["dropped.csv"]
    cur = srv.get("/api/case/current")
    assert cur["temp"] is True and cur["path"] != target


def test_a_drag_that_is_not_files_is_left_alone(home_with_a_case):
    """Dragging selected text across the home screen is not an import: it
    is not accepted, nothing lights up, and a drop opens nothing."""
    srv, pg, _ = home_with_a_case
    for selector in (ROW, "#home .home-foot"):
        assert not _drop(pg, selector, kind="text"), selector
    assert not pg.evaluate("(s) => document.querySelector(s).classList.contains('home-case-drop')", ROW)
    assert not pg.evaluate("() => document.getElementById('home').classList.contains('home-drop')")
    # Nothing to wait for: the check is that neither drop opened anything.
    pg.wait_for_timeout(300)
    assert pg.locator("#home").is_visible() and pg.locator("#app").is_hidden()
    assert srv.get("/api/case/current")["open"] is False
