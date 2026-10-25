"""A long drag of the scrollbar shows the rows it stops on without waiting
for the pages it passed over.

Every frame of a thumb drag lands on a different 5,000-row page, and the
grid used to ask for each of them. Nothing cancelled the pages the thumb
had already left, the browser sends six requests to one host at a time,
and the page the analyst let go on queued behind all of them: on a
1M-row EvtxECmd table a one-second drag sent ~31 page requests and showed
its rows ~24 s after the button came up (docs/notes/grid.md).

Stubbed the way test_grid_past_the_spacer_cap.py is: the build says the
view has N rows, and its row fetches are answered here with a row per
position whose line number is position + 1. Once the drag starts, a
request for a whole page is held and never answered — the old queue at
its limit — so a grid that needs those pages to paint where it stopped
never paints there. Nothing reaches the server but the real build."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

pytestmark = pytest.mark.ui

N = 1_000_000
PAGE = 5000


def _path_is(path):
    return lambda url: urlparse(url).path == path


class Stub:
    """The rows of the stubbed view, and the notes saved on them. Once
    `holding` is set, a request for a whole page goes into `held`,
    answered as of when it was asked but not delivered until release();
    every other request for its rows is answered at once."""

    def __init__(self, template):
        self.template = template
        self.holding = False
        self.held = []
        self.notes = {}   # rid -> note

    def rows(self, start, count):
        return {"start": start, "rows": [
            {**self.template, "pos": p, "rid": p + 1, "tags": [], "note": self.notes.get(p + 1)}
            for p in range(start, min(start + count, N))]}

    def held_pages(self):
        return sorted(start // PAGE for start, _, _ in self.held)

    def release(self):
        self.holding = False
        while self.held:
            _, route, answer = self.held.pop(0)
            route.fulfill(json=answer)


@pytest.fixture
def big_view(page):
    """Rebuilds the open table as a view of N rows."""
    stub = Stub(page.evaluate(
        "() => __winnow.api(`/api/rows?view_id=${__winnow.S.view.view_id}&start=0&count=1`)")["rows"][0])
    stubbed = set()

    def build(route):
        resp = route.fetch()
        view = resp.json()
        view["row_count"] = N
        stubbed.add(view["view_id"])
        route.fulfill(response=resp, json=view)

    def rows(route):
        qs = parse_qs(urlparse(route.request.url).query)
        if qs["view_id"][0] not in stubbed:
            route.continue_()
            return
        start, count = int(qs["start"][0]), int(qs["count"][0])
        if stub.holding and count >= PAGE:
            stub.held.append((start, route, stub.rows(start, count)))
        else:
            route.fulfill(json=stub.rows(start, count))

    def note(route):
        # The stubbed rows are not the shared table's, so their notes stay here.
        body = route.request.post_data_json
        stub.notes[body["rid"]] = body["note"] or None
        route.fulfill(json={"ok": True})

    page.route(_path_is("/api/view"), build)
    page.route(_path_is("/api/rows"), rows)
    page.route(_path_is("/api/note"), note)
    page.evaluate("() => __winnow.rebuildView({ keepScroll: false, keepRow: false })")
    page.wait_for_function(
        "(n) => __winnow.S.view && __winnow.S.view.row_count === n && __winnow.busyCount === 0", arg=N)
    page.wait_for_selector('.row[data-pos="0"]:not(.pending)')
    # At rest: the next page prefetched, nothing in flight.
    page.wait_for_function("() => __winnow.S.pages.has(1) && __winnow.S.pending.size === 0")
    yield stub
    stub.holding = False
    for _, route, _ in stub.held:
        route.abort()
    page.unroute_all(behavior="ignoreErrors")


DRAG = """async ([frac, frames]) => {
  const b = document.getElementById('body');
  const to = Math.round((b.scrollHeight - b.clientHeight) * frac);
  for (let i = 1; i <= frames; i++) {
    await new Promise(requestAnimationFrame);
    b.scrollTop = Math.round(to * i / frames);
  }
}"""

# The paint for where the thumb stopped is up, and none of its rows is a
# placeholder.
LANDED = """() => {
  const W = __winnow, b = document.getElementById('body');
  const want = Math.max(0, Math.floor(W.vScroll(b, W.S.view.row_count, W.headH()) / W.ROW_H) - W.OVERSCAN);
  const rows = [...document.querySelectorAll('#rows .row')];
  return rows.length > 0 && Number(rows[0].dataset.pos) === want
      && !rows.some((r) => r.classList.contains('pending'));
}"""


def _drag(page, stub, frac=0.8, frames=30):
    """Drags the thumb from the top to `frac` of its travel, one step a
    frame (a real drag's scroll events), holding every page asked for from
    the first step on, and says whether the rows it stopped on painted.
    A wait that would time out is the failure."""
    stub.holding = True
    page.evaluate(DRAG, [frac, frames])
    try:
        page.wait_for_function(LANDED, timeout=10_000)
        return True
    except PlaywrightTimeout:
        return False


def _on_screen(page):
    """[position, line number] for each row painted."""
    return page.evaluate("() => [...document.querySelectorAll('#rows .row')].map("
                         "(r) => [Number(r.dataset.pos), r.querySelector('.rid').textContent])")


def _pages_on_screen(page):
    rows = _on_screen(page)
    return rows[0][0] // PAGE, rows[-1][0] // PAGE


def test_a_long_drag_paints_where_it_stops_without_the_pages_it_passed(page, big_view):
    landed = _drag(page, big_view)
    asked = big_view.held_pages()
    assert landed, f"the rows the drag stopped on never painted; it asked for pages {asked[:12]}"
    # The rows are the ones their positions say, and none was skipped.
    seen = _on_screen(page)
    assert [str(p + 1) for p, _ in seen] == [rid for _, rid in seen]
    assert [p for p, _ in seen] == list(range(seen[0][0], seen[0][0] + len(seen)))
    assert seen[0][0] > N // 2
    # It passed some 160 pages on the way there and asked for none of them.
    lo, hi = _pages_on_screen(page)
    assert all(lo - 1 <= p <= hi + 1 for p in asked), asked


def test_the_page_follows_once_the_drag_has_stopped(page, big_view, until):
    assert _drag(page, big_view)
    lo, hi = _pages_on_screen(page)
    # Asked for from where the grid came to rest, so scrolling on from
    # there reads the page cache as it always did.
    until(page, lambda: {lo, hi} <= set(big_view.held_pages()), "the pages under the rows it stopped on")
    big_view.release()
    page.wait_for_function("([lo, hi]) => __winnow.S.pages.has(lo) && __winnow.S.pages.has(hi)", arg=[lo, hi])
    page.wait_for_function(LANDED)
    seen = _on_screen(page)
    assert [str(p + 1) for p, _ in seen] == [rid for _, rid in seen]


def test_a_note_written_before_its_page_lands_is_still_there_after(page, big_view, until):
    """The rows a drag stops on can be opened and written to before their
    page lands, and that page was asked for at the settle — before the
    note was saved. Landing over the row, its answer put the old, empty
    note on the row and in the note box (the save itself had worked)."""
    assert _drag(page, big_view)
    pos = page.evaluate("() => __winnow.paintWindow().first + 20")
    until(page, lambda: pos // PAGE in big_view.held_pages(), "the page under the row")
    page.locator(f".row[data-pos='{pos}'] .cell").first.click()
    page.evaluate("(p) => __winnow.showDetail(p)", pos)
    page.wait_for_function("(rid) => document.getElementById('noteInput').dataset.rid === String(rid)", arg=pos + 1)
    page.locator("#noteInput").type("seen at 03:12")
    page.wait_for_function("() => document.getElementById('noteStatus').textContent === 'Saved'")
    assert big_view.notes == {pos + 1: "seen at 03:12"}
    big_view.release()   # the pre-note answer included
    page.wait_for_function("(i) => __winnow.S.pages.has(i)", arg=pos // PAGE)
    assert page.locator("#noteInput").input_value() == "seen at 03:12"
    assert page.evaluate("(p) => __winnow.rowAt(p).note", pos) == "seen at 03:12"
