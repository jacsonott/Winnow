"""Which rows the derive preview says it sampled, and how to change them.

The preview sampled the head of the file, which is the one region a
triage filter exists to escape — so it could report "All 200 sampled
values parse" about rows the analyst had filtered away, and the mismatch
only showed up after a backfill over millions of rows.

The control mirrors the value picker's scope segment, because it is the
same question asked about a different thing. What can only be checked in
a browser is that the modal arrives on the right side of it — a default
that reads the analyst's state wrongly is worse than no default, since
the verdict looks authoritative either way.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_derive(page, column="CommandLine"):
    page.evaluate("(c) => __winnow.openDerivedColumnModal(c)", column)
    page.wait_for_selector("#modal:not([hidden])")
    page.wait_for_selector(".derived-scope .btn")


def _scope(page):
    return page.evaluate("""() => [...document.querySelectorAll('.derived-scope .btn')]
      .filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.dataset.scope)""")


def _verdict(page):
    page.wait_for_selector(".derived-verdict", timeout=15_000)
    return page.locator(".derived-verdict").inner_text()


@pytest.fixture(autouse=True)
def cleanup(page):
    yield
    page.keyboard.press("Escape")
    page.evaluate("() => __winnow.clearAllFilters()")
    page.wait_for_function("() => __winnow.busyCount === 0", timeout=15_000)


def test_an_unfiltered_table_previews_against_the_whole_table(page):
    """Nothing has been narrowed, so there is nothing to scope to — and
    the cheaper head-of-file read says the same thing."""
    _open_derive(page)
    assert _scope(page) == ["table"]
    assert "whole table" in _verdict(page)


def test_a_filtered_table_previews_against_the_view(page):
    """The analyst has already said which rows they mean."""
    page.evaluate("""() => { __winnow.S.filters = { EventId: '=4624' };
      return __winnow.rebuildView({ keepScroll: false }); }""")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count < 200", timeout=15_000)
    _open_derive(page)
    assert _scope(page) == ["view"]
    # The verdict names the sample it is a verdict about: "All 200 sampled
    # values parse" is a different claim depending on which 200.
    assert "in this view" in _verdict(page)


def test_the_scope_can_be_switched_and_the_verdict_follows(page):
    page.evaluate("""() => { __winnow.S.filters = { EventId: '=4624' };
      return __winnow.rebuildView({ keepScroll: false }); }""")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count < 200", timeout=15_000)
    _open_derive(page)
    assert "in this view" in _verdict(page)

    page.locator('.derived-scope .btn[data-scope="table"]').click()
    page.wait_for_function(
        # Guarded: refreshPreview clears the box to "Checking…" first, so
        # the verdict node is briefly absent rather than merely stale.
        "() => { const n = document.querySelector('.derived-verdict');"
        " return !!n && /whole table/.test(n.textContent); }",
        timeout=15_000)
    assert _scope(page) == ["table"]


def test_the_view_scope_actually_reaches_the_server(page):
    """The label is not the claim — the request is. A control that read
    correctly and sent nothing would pass every assertion above."""
    page.evaluate("""() => { __winnow.S.filters = { EventId: '=4624' };
      return __winnow.rebuildView({ keepScroll: false }); }""")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count < 200", timeout=15_000)
    sent = []
    page.route("**/api/derived/preview", lambda route: (
        sent.append(route.request.post_data_json), route.continue_()))
    try:
        _open_derive(page)
        _verdict(page)
        assert sent, "no preview was requested"
        assert sent[-1].get("view_id") == page.evaluate("() => __winnow.S.view.view_id"), sent[-1]

        page.locator('.derived-scope .btn[data-scope="table"]').click()
        page.wait_for_function(
            # Guarded: refreshPreview clears the box to "Checking…" first, so
        # the verdict node is briefly absent rather than merely stale.
        "() => { const n = document.querySelector('.derived-verdict');"
        " return !!n && /whole table/.test(n.textContent); }",
            timeout=15_000)
        assert sent[-1].get("view_id") in (None, ""), sent[-1]
    finally:
        page.unroute("**/api/derived/preview")
