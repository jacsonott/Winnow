"""Adding a coalesce column through the modal: the Combine type, the
ordered column chips, and the column arriving in the grid."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui


def test_combine_type_offers_coalesce_and_the_chips_build_the_param(page):
    page.evaluate("() => __winnow.openDerivedColumnModal('Host')")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modalBody select").nth(0).select_option(label="Combine columns")   # type
    page.wait_for_timeout(150)
    ops = page.evaluate(
        "() => [...document.querySelectorAll('#modalBody select')[2].options].map((o) => o.textContent)")
    assert ops == ["First non-empty value"], ops
    assert page.locator(".derived-name").input_value() == "Host (combined)"
    # The chips widget, not a select: order is the meaning.
    add = page.locator(".derived-columns select.fb-groupby-add")
    assert add.count() == 1
    assert page.locator(".derived-columns .fb-groupby-chip").count() == 0
    add.select_option("EventId")
    page.wait_for_timeout(100)
    page.locator(".derived-columns select.fb-groupby-add").select_option("CommandLine")
    page.wait_for_timeout(100)
    chips = page.locator(".derived-columns .fb-groupby-chip").all_inner_texts()
    assert [c.replace("✕", "").strip() for c in chips] == ["EventId", "CommandLine"]
    # The chosen ones (and the parse column itself) leave the add list.
    left = page.evaluate(
        "() => [...document.querySelector('.derived-columns select.fb-groupby-add').options].map((o) => o.value)")
    assert "EventId" not in left and "CommandLine" not in left and "Host" not in left
    # Clicking the row's label text, or a chip's name, must not remove a
    # chip (a <label> would forward that click to the first ✕).
    page.locator(".derived-param-label", has_text="Then try").click()
    page.locator(".derived-columns .fb-groupby-chip").first.click(position={"x": 8, "y": 6})
    assert page.locator(".derived-columns .fb-groupby-chip").count() == 2
    # Changing the Parse column keeps the chosen Type and its chips.
    page.locator("#modalBody select").nth(1).select_option("Timestamp")
    page.wait_for_timeout(400)
    assert page.locator("#modalBody select").nth(0).input_value() == "Combine columns"
    assert page.locator(".derived-columns .fb-groupby-chip").count() == 2
    page.locator("#modalBody select").nth(1).select_option("Host")
    page.wait_for_timeout(300)
    # Removing the first chip keeps the second in place.
    page.locator(".derived-columns .fb-groupby-rm").first.click()
    chips = page.locator(".derived-columns .fb-groupby-chip").all_inner_texts()
    assert [c.replace("✕", "").strip() for c in chips] == ["CommandLine"]

    page.locator(".derived-name").fill("HostOrProcess")
    page.wait_for_selector(".derived-preview-row")
    page.click("#modalBody .btn:has-text('Add column')")
    page.wait_for_selector("#modal", state="hidden")
    page.wait_for_function(
        "() => __winnow.S.columns.some((c) => c.name === 'HostOrProcess' && c.derived)", timeout=15_000)
    did = None
    try:
        # The backfill is a job; poll its definition from here rather than
        # from a promise-returning predicate.
        for _ in range(100):
            d = page.evaluate("""() => fetch('/api/derived?source_id=' + __winnow.S.sourceId,
              { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())
              .then((defs) => defs.find((x) => x.name === 'HostOrProcess') || null)""")
            if d and d.get("status") == "ready":
                did = d["id"]
                break
            time.sleep(0.1)
        assert d and d["status"] == "ready", d
        assert d["params"] == {"extra_columns": ["CommandLine"]}, d["params"]
        # In the column order (the header window may not reach the last
        # column on this viewport, so the DOM isn't the thing to ask).
        assert page.evaluate("() => __winnow.S.order.includes('HostOrProcess')")
    finally:
        if did:
            page.evaluate("""(id) => fetch('/api/derived/' + id, { method: 'DELETE',
              headers: { 'X-Timeline-Lite-Client': '1' } })""", did)
        page.evaluate("() => __winnow.loadSources()")
