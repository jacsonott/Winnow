"""Every row of a view taller than the grid's spacer can be is reachable.

A DOM element can't be arbitrarily tall, so the spacer that gives #body
its scroll height is capped (MAX_SPACER_PX), and above about 1.4M rows
scrollTop stops being a row offset: every conversion goes through
vScroll/rScroll and the rows are placed by rowsPaintY (docs/notes/grid.md).
Before those existed, a 2,459,653-row $J scrolled only as far as row
~1,398,090, and nothing on screen said that 43% of it was missing.

The shared table has 200 rows, so these tests stub the size rather than
ingest 2.5M: the build's answer is rewritten to say the view has N rows,
and the page fetches for that view are answered here with a row per
position whose line number is position + 1. Nothing reaches the server
but the real builds, so there is nothing to put back afterwards."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.ui

N = 2_500_000


def _path_is(path):
    return lambda url: urlparse(url).path == path


@pytest.fixture
def tall_view(page):
    """Rebuilds the open table as a view of N rows."""
    template = page.evaluate(
        "() => __winnow.api(`/api/rows?view_id=${__winnow.S.view.view_id}&start=0&count=1`)")["rows"][0]
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
        route.fulfill(json={"start": start, "rows": [
            {**template, "pos": p, "rid": p + 1, "tags": [], "note": None}
            for p in range(start, min(start + count, N))]})

    page.route(_path_is("/api/view"), build)
    page.route(_path_is("/api/rows"), rows)
    page.evaluate("() => __winnow.rebuildView({ keepScroll: false, keepRow: false })")
    page.wait_for_function(
        "(n) => __winnow.S.view && __winnow.S.view.row_count === n && __winnow.busyCount === 0", arg=N)
    page.wait_for_selector('.row[data-pos="0"]:not(.pending)')
    yield
    page.unroute_all(behavior="ignoreErrors")


def _first_painted(page):
    return page.evaluate("() => Number(document.querySelector('#rows .row').dataset.pos)")


def _settled(page, before):
    """Waits for the grid to repaint away from row `before` and for every
    row it painted to have its data. Deliberately not a wait for the row a
    test expects, so a grid that cannot get there fails on the assertion
    that says where it got instead of on a timeout."""
    page.wait_for_function("""(before) => {
        const rows = [...document.querySelectorAll('#rows .row')];
        return rows.length > 0 && Number(rows[0].dataset.pos) !== before
            && !rows.some((r) => r.classList.contains('pending'));
    }""", arg=before)


def _scroll_to(page, frac):
    """Drags the scrollbar to `frac` of its travel and waits for the paint."""
    before = _first_painted(page)
    page.evaluate("(f) => { const b = document.getElementById('body');"
                  " b.scrollTop = Math.round((b.scrollHeight - b.clientHeight) * f); }", frac)
    _settled(page, before)


def _on_screen(page):
    """The loaded rows whose whole height is inside the grid's viewport,
    below its sticky header: [position, line number] for each, plus the
    gap between the lowest one and the viewport's foot."""
    return page.evaluate("""() => {
        const b = document.getElementById('body');
        const r = b.getBoundingClientRect();
        const top = r.top + __winnow.headH(), foot = r.top + b.clientHeight;
        const rows = [...document.querySelectorAll('#rows .row:not(.pending)')].filter((row) => {
            const g = row.getBoundingClientRect();
            return g.top >= top - 0.5 && g.bottom <= foot + 0.5;
        });
        const last = rows[rows.length - 1];
        return {
            rows: rows.map((row) => [Number(row.dataset.pos), row.querySelector('.rid').textContent]),
            gap: last ? foot - last.getBoundingClientRect().bottom : null,
        };
    }""")


def test_the_scrollbar_reaches_the_last_row(page, tall_view):
    _scroll_to(page, 1)
    seen = _on_screen(page)
    # The last row is on screen, whole, at the foot of the grid, and it is
    # the row its position says: its line number is N.
    assert seen["rows"][-1] == [N - 1, str(N)], seen["rows"][-3:]
    assert 0 <= seen["gap"] < 1, seen
    # Its neighbours run on without a hole, so nothing was skipped to get here.
    assert [p for p, _ in seen["rows"]] == list(range(seen["rows"][0][0], N))
    # The spacer stayed under the cap: past it is where the browser stops
    # growing the element and the tail becomes unreachable. Uncapped, this
    # view's spacer would be 60M px, which the browser clamps at about 33.5M.
    # A viewport of slack: the header's height and the rows' fractional
    # placement round differently with different fonts.
    height = page.evaluate("() => { const b = document.getElementById('body');"
                           " return [b.scrollHeight, __winnow.MAX_SPACER_PX + b.clientHeight]; }")
    assert height[0] <= height[1], height


def test_halfway_down_the_scrollbar_is_halfway_down_the_view(page, tall_view):
    # Without the rescale, half of a capped scrollbar is row ~333,000 of
    # 2.5M, and nothing past row ~666,000 can be reached at all.
    _scroll_to(page, 0.5)
    first = _on_screen(page)["rows"][0][0]
    assert abs(first - N // 2) < 100, first


def test_the_last_row_key_lands_on_the_last_row(page, tall_view):
    page.locator('.row[data-pos="0"] .cell').first.click()
    page.keyboard.press("Control+ArrowDown")
    _settled(page, 0)
    assert page.evaluate("() => __winnow.S.cursor") == N - 1
    seen = _on_screen(page)
    assert seen["rows"][-1] == [N - 1, str(N)], seen["rows"][-3:]
    # And back: the first row is just as reachable from the end.
    before = _first_painted(page)
    page.keyboard.press("Control+ArrowUp")
    _settled(page, before)
    assert page.evaluate("() => __winnow.S.cursor") == 0
    assert _on_screen(page)["rows"][0] == [0, "1"]
