"""A rebuild paints from the rows it lands on, not from their page.

runBuild fetches the rows where the grid will land before it swaps
anything, so the old rows stay up until the new ones replace them in one
paint. That fetch used to be the whole page — on a 27-column EvtxECmd
table 5,000 rows are 7.8 MB, 110–140 ms to serve and about as long to
parse, which was most of what a fast search waited on once its build was
done. It is the screenful now, put up as a glimpse (grid.js
seedGlimpse), and the page follows in the background, the way it does
after a jump (docs/notes/grid.md).
"""
from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui

PAGE_FETCH = re.compile(r".*/api/rows\?.*&count=5000$")


def test_a_rebuild_paints_before_its_page_lands(page, until):
    held = []
    page.route(PAGE_FETCH, lambda route: held.append(route))
    box = page.locator('.fcell input[data-col="EventId"]')
    try:
        box.fill("4624")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 50 && __winnow.busyCount === 0")
        # Painted from the seed, with the page still held: real rows, none
        # of them a placeholder, and nothing in the page cache yet.
        page.wait_for_function("""() => {
          const rows = [...document.querySelectorAll('#rows .row')];
          return rows.length > 0 && !rows.some((r) => r.classList.contains('pending'));
        }""")
        assert page.evaluate("() => __winnow.S.pages.size") == 0
        assert page.evaluate("() => __winnow.rowAt(0).cells.includes('4624')")
        # ...and the paint asked for the page.
        until(page, lambda: held, "the page was never asked for")
        for r in held:
            r.continue_()
        held.clear()
        page.wait_for_function("() => __winnow.S.pages.has(0)")
        assert page.evaluate("() => __winnow.S.pages.get(0).length") == 50
    finally:
        page.unroute(PAGE_FETCH)
        for r in held:
            try:
                r.continue_()
            except Exception:
                pass
        box.fill("")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200 && __winnow.busyCount === 0")
