"""A page that comes back "view expired" starts one rebuild, not one per repaint.

The grid's recovery for an expired view is to rebuild it. When that
rebuild failed too — the table was removed from another window, or an
edited merge no longer has the column a filter names — the view on
screen stayed the expired one, the failure repainted the grid, the
repaint asked for the same page, and the 409 started the same rebuild
again: a hundred-odd failing requests a second, with no input from the
analyst, until the page was reloaded.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui


def test_a_view_that_cannot_be_rebuilt_is_not_retried_forever(page, server_post, server_call, tmp_path):
    f = tmp_path / "removed-elsewhere.csv"
    # More than one page (5000 rows), so scrolling asks for a page the
    # grid has not fetched yet.
    f.write_text("Proc,User\n" + "".join(f"p{i % 3},u{i % 2}\n" for i in range(12_000)), encoding="utf-8")
    server_post("/api/ingest/jobs/path", {"path": str(f), "name": f.name, "kind": "csv"})
    deadline = time.monotonic() + 25
    sid = None
    while sid is None and time.monotonic() < deadline:
        sid = page.evaluate("""(name) => __winnow.loadSources().then(() => {
            const s = __winnow.S.sources.find((x) => x.name === name);
            return s ? s.id : null; })""", f.name)
        if sid is None:
            page.wait_for_timeout(250)
    assert sid is not None, f"{f.name} never appeared"
    try:
        page.evaluate("(id) => __winnow.openSource(id)", sid)
        page.wait_for_function(
            "(id) => __winnow.S.sourceId === id && __winnow.S.view && __winnow.busyCount === 0", arg=sid)

        sent = []
        page.on("request", lambda r: sent.append(r.url) if any(
            p in r.url for p in ("/api/rows", "/api/view")) else None)
        # The recovery: the page 409s, the grid rebuilds, the rebuild fails.
        # Listening starts before the table goes, because the scroll is not
        # the only thing that asks for a page: the idle prefetch of the next
        # one can land between the DELETE and the scroll, spend the view's
        # one rebuild there, and leave the scroll's 409 (rightly) starting
        # none, so a wait opened after the DELETE never saw a response.
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/view")) as rebuilt:
            # From outside the page: another window removing the table.
            server_call(f"/api/source/{sid}", method="DELETE")
            sid = None
            page.locator("#body").evaluate("(b) => { b.scrollTop = b.scrollHeight; }")
        assert rebuilt.value.status >= 400
        # Nothing to wait for: the failure is requests that keep coming.
        # Two seconds of the loop sent a few hundred of them.
        page.wait_for_timeout(2000)
        assert len(sent) <= 6, f"{len(sent)} requests after the table went: {sent[:8]}"
    finally:
        if sid is not None:
            server_call(f"/api/source/{sid}", method="DELETE")
