"""A table with a lot of columns stays as fast as a narrow one.

Measured on a 150-column table, before: every grid paint built a cell for
every column of every painted row — 8,550 cells for the ~10 columns on
screen across — and a scroll step, an arrow key or a cell click each
waited ~470 ms on it (a 20-column table: ~75 ms). The SQL pane was worse:
`SELECT *` on the same table put 750,000 cells in one <table>, took 45 s
to appear and rebuilt all of it on every row click (42 s each).

Both now build only what is on screen. The tests below assert that in the
terms it went wrong in — how many cells a paint builds against how many
columns the table has — and, since building less is only a fix if
nothing on screen goes missing, that every column the viewport shows has
its cell, at its header's x, wherever the grid or the pane is scrolled to.
No timings: a required check that depends on the runner's speed is the
kind people learn to re-run until it's green.

Boots its own server on a generated wide table rather than adding one to
the shared fixture's case, whose table list other modules count on.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

ROOT = Path(__file__).resolve().parent.parent.parent
N_COLS = 150
N_ROWS = 3000


@pytest.fixture(scope="module")
def wide_server(tmp_path_factory):
    d = tmp_path_factory.mktemp("wide")
    csv = d / "wide.csv"
    head = ["Timestamp", "EventId", "Host"] + [f"Field_{i:03d}" for i in range(N_COLS - 3)]
    lines = [",".join(head)]
    for r in range(N_ROWS):
        lines.append(",".join(
            [f"2026-03-14 08:{r // 60 % 60:02d}:{r % 60:02d}", str(4624 + r % 3), f"H{r % 7}"]
            + [f"v{r}-{c}" for c in range(N_COLS - 3)]))
    csv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--case", str(d / "wide.db"), "--open", str(csv),
         "--port", str(port), "--host", "127.0.0.1", "--no-browser", "--no-fts"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**os.environ,
             "WINNOW_WORKSPACE_DIR": str(d / "ws"), "WINNOW_ENV_FILE": str(d / "env"),
             "XDG_DATA_HOME": str(d / "xdg-data"), "XDG_CONFIG_HOME": str(d / "xdg-config")})
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail("server exited during startup:\n" + proc.stdout.read().decode(errors="replace"))
        try:
            urllib.request.urlopen(base + "/api/sources", timeout=1)
            break
        except OSError:
            time.sleep(0.15)
    else:
        proc.kill()
        pytest.fail("server did not come up")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def wide(browser, wide_server, first_run_init):
    ctx = browser.new_context(viewport={"width": 1500, "height": 900},
                              permissions=["clipboard-read", "clipboard-write"])
    ctx.add_init_script(first_run_init)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(wide_server, wait_until="networkidle")
    pg.wait_for_selector("#rows .row:not(.pending)", timeout=30_000)
    yield pg
    ctx.close()
    assert not errors, "uncaught JS errors: " + " | ".join(errors)


def _settle(page):
    """Two frames: the scroll listener paints on the next one."""
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")


# What a painted grid row says about itself: how many cells it carries,
# every on-screen header that has no cell under it, and every cell that is
# not at its header's x and width.
_GRID_ROW = """() => {
  const body = document.getElementById('body');
  const box = body.getBoundingClientRect();
  const cols = __winnow.visibleCols();
  const row = [...document.querySelectorAll('#rows .row:not(.pending)')][4];
  const cells = [...row.querySelectorAll('.cell')];
  const have = new Set(cells.map((c) => cols[Number(c.dataset.col)]));
  const missing = [], misplaced = [];
  for (const h of document.querySelectorAll('#headRow .hcell[data-col]')) {
    const b = h.getBoundingClientRect();
    if (b.right > box.left && b.left < box.right && !have.has(h.dataset.col)) missing.push(h.dataset.col);
  }
  for (const c of cells) {
    const name = cols[Number(c.dataset.col)];
    const a = c.getBoundingClientRect();
    const b = document.querySelector(`#headRow .hcell[data-col="${CSS.escape(name)}"]`).getBoundingClientRect();
    if (Math.abs(a.left - b.left) > 0.5 || Math.abs(a.width - b.width) > 0.5) misplaced.push(name);
  }
  return { cells: cells.length, missing, misplaced, rowWidth: row.getBoundingClientRect().width,
           headWidth: document.getElementById('headRow').getBoundingClientRect().width };
}"""


# ----------------------------------------------------------------- grid

def test_a_paint_builds_the_columns_on_screen_not_the_whole_table(wide):
    assert wide.evaluate("() => __winnow.visibleCols().length") == N_COLS
    for where in ("0", "body.scrollWidth / 2", "body.scrollWidth"):
        wide.evaluate(f"() => {{ const body = document.getElementById('body'); body.scrollLeft = {where}; }}")
        _settle(wide)
        got = wide.evaluate(_GRID_ROW)
        assert got["cells"] < N_COLS / 3, f"scrollLeft={where}: a row built {got['cells']} of {N_COLS} cells"
        assert not got["missing"], f"scrollLeft={where}: on screen with no cell under it: {got['missing']}"
        assert not got["misplaced"], f"scrollLeft={where}: cells off their header: {got['misplaced']}"
        # The stripe still runs the full width: a row ending where its
        # last built cell does would leave the rest of the table bare.
        assert abs(got["rowWidth"] - got["headWidth"]) < 1


def test_a_pinned_column_holds_its_place_however_far_the_grid_scrolls(wide):
    wide.evaluate("() => __winnow.togglePin('Host')")
    try:
        wide.evaluate("() => { const b = document.getElementById('body'); b.scrollLeft = b.scrollWidth / 2; }")
        _settle(wide)
        got = wide.evaluate("""() => {
          const ci = __winnow.visibleCols().indexOf('Host');
          const cell = [...document.querySelectorAll('#rows .row:not(.pending)')][4].querySelector(`.cell[data-col="${ci}"]`);
          const head = document.querySelector('#headRow .hcell[data-col="Host"]');
          return { cell: cell && cell.getBoundingClientRect().left, head: head.getBoundingClientRect().left,
                   body: document.getElementById('body').getBoundingClientRect().left };
        }""")
        assert got["cell"] is not None, "the pinned column's cell was not built once its place scrolled away"
        assert abs(got["cell"] - got["head"]) < 0.5
        assert got["cell"] - got["body"] < 200, "the pinned cell scrolled away instead of sticking"
        assert not wide.evaluate(_GRID_ROW)["missing"]
    finally:
        wide.evaluate("() => __winnow.togglePin('Host')")


def test_the_grid_growing_without_a_window_resize_paints_the_new_columns(wide):
    """Closing the detail pane or folding the sidebar widens the grid with
    no scroll and no window `resize` — and the rows only carry the columns
    the narrower grid could see."""
    wide.evaluate("() => { document.getElementById('body').style.marginRight = '900px'; __winnow.render(); }")
    _settle(wide)
    narrow = wide.evaluate(_GRID_ROW)["cells"]
    wide.evaluate("() => { document.getElementById('body').style.marginRight = ''; }")
    _settle(wide)
    got = wide.evaluate(_GRID_ROW)
    assert got["cells"] > narrow
    assert not got["missing"], f"columns uncovered by the wider grid with no cells: {got['missing']}"


def test_the_arrow_keys_walk_into_columns_that_were_never_built(wide):
    wide.locator("#rows .row:not(.pending)").nth(2).locator(".cell").first.click()
    for _ in range(80):
        wide.keyboard.press("ArrowRight")
    _settle(wide)
    got = wide.evaluate("""() => {
      const a = document.querySelector('#rows .cell-active');
      const box = document.getElementById('body').getBoundingClientRect();
      const r = a && a.getBoundingClientRect();
      return { col: a && Number(a.dataset.col), focus: __winnow.S.cellFocus.col,
               onScreen: !!a && r.left >= box.left && r.right <= box.right + 1 };
    }""")
    assert got["focus"] == 80
    assert got["col"] == 80 and got["onScreen"], got


# ---------------------------------------------------------------- SQL pane

def _landed(page):
    """Until every row the pane has built holds its row: the result is the
    server's, and a scroll past the rows fetched so far paints them empty
    for the moment it takes to fetch them."""
    page.wait_for_function("() => !document.querySelector('#sqlResult tr.sql-row-pending')")


def _run_sql(page, sql):
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    page.wait_for_function("() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled")
    page.locator("#sqlText").fill(sql)
    page.click("#btnRunSql")
    page.wait_for_selector("#sqlResult table")
    _settle(page)


# The SQL result's equivalent of _GRID_ROW, for the row nearest the middle
# of the pane: its rid against the one its place in the table means, the
# cells it built, and any on-screen header with no cell under it.
_SQL_ROW = """() => {
  const sc = document.querySelector('#sqlResult .sql-result-scroll');
  const box = sc.getBoundingClientRect();
  const ths = [...sc.querySelectorAll('th')];
  const mid = (box.top + box.bottom) / 2;
  const tr = [...sc.querySelectorAll('tr[data-i]')].find((t) => {
    const b = t.getBoundingClientRect(); return b.top <= mid && b.bottom > mid; });
  const cells = [], missing = [], misplaced = [];
  let ci = 0;
  for (const td of tr.children) {
    if (td.classList.contains('sql-gap')) { ci += td.colSpan; continue; }
    const a = td.getBoundingClientRect(), b = ths[ci].getBoundingClientRect();
    if (Math.abs(a.left - b.left) > 0.5 || Math.abs(a.width - b.width) > 0.5) misplaced.push(ths[ci].textContent);
    cells.push(ci++);
  }
  ths.forEach((th, i) => {
    const b = th.getBoundingClientRect();
    if (b.right > box.left && b.left < box.right && !cells.includes(i)) missing.push(th.textContent);
  });
  // ORDER BY rid: the row at index i is rid i + 1, and sits i rows under
  // the first data row — which is where the header ends, read off the
  // table rather than the sticky header itself.
  const origin = sc.querySelector('table').getBoundingClientRect().top + ths[0].offsetHeight;
  const drift = Math.round((tr.getBoundingClientRect().top - origin) / tr.offsetHeight) - Number(tr.dataset.i);
  return { rid: Number(tr.children[0].classList.contains('sql-gap') ? NaN : tr.children[0].textContent),
           index: Number(tr.dataset.i), drift, cells: cells.length, missing, misplaced,
           tds: sc.querySelectorAll('td').length,
           widths: ths.slice(0, 8).map((th) => Math.round(th.getBoundingClientRect().width)).join(',') };
}"""


def test_a_large_sql_result_builds_only_what_the_pane_shows(wide):
    _run_sql(wide, "SELECT rid, * FROM src_1 ORDER BY rid")
    assert wide.locator("#sqlResult th").count() > N_COLS
    top = wide.evaluate(_SQL_ROW)
    # 3,000 rows x 152 columns is 456,000 cells; the pane shows a few hundred.
    assert top["tds"] < 5000, f"{top['tds']} cells built for one screen of result"
    for where in ("sc.scrollHeight / 2", "sc.scrollHeight"):
        wide.evaluate(f"() => {{ const sc = document.querySelector('#sqlResult .sql-result-scroll'); sc.scrollTop = {where}; }}")
        _settle(wide)
        _landed(wide)
        got = wide.evaluate(_SQL_ROW)
        assert got["rid"] == got["index"] + 1, f"row {got['index']} on screen holds rid {got['rid']}"
        assert got["drift"] == 0, f"row {got['index']} is painted {got['drift']} rows from its place"
        assert not got["missing"] and not got["misplaced"], got
        # The columns are sized from the whole result, not from whichever
        # rows happen to be built — scrolling must not re-size them.
        assert got["widths"] == top["widths"]
    wide.evaluate("() => { const sc = document.querySelector('#sqlResult .sql-result-scroll'); sc.scrollLeft = sc.scrollWidth / 2; }")
    _settle(wide)
    got = wide.evaluate(_SQL_ROW)
    assert got["cells"] < N_COLS / 3, f"a scrolled row built {got['cells']} cells"
    assert not got["missing"] and not got["misplaced"], got


def _click_row(page, i, shift=False):
    """A click on the row's first built cell, dispatched rather than
    Playwright's: a real click aims at the middle of the element, which on
    a 150-column row is thousands of pixels to the right, and the scroll
    it makes to get there would repaint the very rows this is about."""
    page.evaluate("""([i, shift]) => document.querySelector(`#sqlResult tr[data-i="${i}"] td:not(.sql-gap)`)
        .dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: shift }))""", [i, shift])


def test_a_row_click_in_a_large_result_moves_the_highlight_and_nothing_else(wide, copied):
    """Every click used to rebuild the whole table to move one class."""
    _run_sql(wide, "SELECT rid, * FROM src_1 ORDER BY rid")
    wide.evaluate("() => document.querySelectorAll('#sqlResult tr[data-i]').forEach((t) => { t.__kept = true; })")
    _click_row(wide, 3)
    assert wide.locator("#sqlResult .sql-row-sel").count() == 1
    assert wide.evaluate("() => [...document.querySelectorAll('#sqlResult tr[data-i]')].every((t) => t.__kept)"), \
        "the click rebuilt the result's rows"
    # A Shift range reaches rows that were not built when it started, and
    # it is the whole range that the selection holds.
    wide.evaluate("() => { document.querySelector('#sqlResult .sql-result-scroll').scrollTop = 20000; }")
    _settle(wide)
    _landed(wide)
    far = wide.evaluate("() => Number(document.querySelectorAll('#sqlResult tr[data-i]')[30].dataset.i)")
    _click_row(wide, far, shift=True)
    assert wide.locator("#sqlResult .sql-row-sel").count() > 20
    wide.evaluate("() => document.activeElement && document.activeElement.blur()")
    with copied(wide) as clip:
        wide.keyboard.press("Control+c")
    assert len(clip.text.strip().splitlines()) == 1 + (far - 3 + 1), "header + every row of the range"
    # ...and the rows scrolled back to come up picked.
    wide.evaluate("() => { document.querySelector('#sqlResult .sql-result-scroll').scrollTop = 0; }")
    _settle(wide)
    picked = wide.evaluate("() => [...document.querySelectorAll('#sqlResult tr[data-i]')].slice(0, 6)"
                           ".map((t) => t.classList.contains('sql-row-sel'))")
    assert picked == [False, False, False, True, True, True]
    wide.keyboard.press("Escape")
