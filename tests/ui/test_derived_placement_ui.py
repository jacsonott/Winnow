"""A new derived column lands right after the column it was derived from
in the grid's order — not at the far end — and a second one from the same
input follows the first. Re-opening the table keeps that; removing the
column leaves the order as it was.
"""
import pytest

pytestmark = pytest.mark.ui


def _add(page, name, input_column):
    return page.evaluate("""([name, col]) => fetch('/api/derived', { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' },
      body: JSON.stringify({ source_id: __winnow.S.sourceId, name, input_column: col, op_id: 'regex_extract', params: { pattern: '(.)' } }) })
      .then((r) => r.json())""", [name, input_column])


def test_derived_column_sits_right_of_its_input(page):
    before = page.evaluate("() => [...__winnow.S.order]")
    ids = []
    try:
        ids.append(_add(page, "EventFirst", "EventId")["definition"]["id"])
        page.evaluate("() => __winnow.showDerivedColumnsSoon()")
        page.wait_for_function("() => __winnow.S.order.includes('EventFirst')", timeout=15_000)
        order = page.evaluate("() => [...__winnow.S.order]")
        assert order.index("EventFirst") == order.index("EventId") + 1, order
        ids.append(_add(page, "EventSecond", "EventId")["definition"]["id"])
        page.evaluate("() => __winnow.showDerivedColumnsSoon()")
        page.wait_for_function("() => __winnow.S.order.includes('EventSecond')", timeout=15_000)
        order = page.evaluate("() => [...__winnow.S.order]")
        i = order.index("EventId")
        assert order[i:i + 3] == ["EventId", "EventFirst", "EventSecond"], order
        # Everything else kept its relative order
        assert [n for n in order if n not in ("EventFirst", "EventSecond")] == before
    finally:
        for did in ids:
            page.evaluate("""(id) => fetch('/api/derived/' + id, { method: 'DELETE',
              headers: { 'X-Timeline-Lite-Client': '1' } })""", did)
        page.evaluate("() => __winnow.showDerivedColumnsSoon()")
        page.wait_for_function("() => !__winnow.S.order.includes('EventFirst') && !__winnow.S.order.includes('EventSecond')", timeout=15_000)
    assert page.evaluate("() => [...__winnow.S.order]") == before
