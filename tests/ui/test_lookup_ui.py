"""The lookup derived column, driven through the real modal. The backend
tests prove the mapping; what only a browser can prove is the modal's
dependent pickers — the table list excluding the open table, and the
key/value column selects re-listing when the chosen table changes. That
rebuild happens inside a change handler that also wipes the stale picks,
which is exactly the kind of wiring a refactor can drop with every backend
test still green."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _ingest(page, path):
    status = page.evaluate("""(path) => fetch('/api/ingest/path', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }) }).then((r) => r.status)""", str(path))
    assert status == 200


def test_lookup_modal_pickers_preview_and_column(page, tmp_path):
    owners = tmp_path / "owners.csv"
    owners.write_text("Host,Owner\nH0,alice\nH1,bob\nH2,carol\nH3,dave\nH4,erin\n", encoding="utf-8")
    sites = tmp_path / "sites.csv"
    sites.write_text("Machine,Site\nH0,HQ\nH1,Branch\n", encoding="utf-8")
    _ingest(page, owners)
    _ingest(page, sites)
    page.evaluate("() => __winnow.loadSources()")
    page.wait_for_function(
        "() => ['owners.csv', 'sites.csv'].every((n) => __winnow.S.sources.some((s) => s.name === n))")
    ids = page.evaluate("() => Object.fromEntries(__winnow.S.sources.map((s) => [s.name, s.id]))")
    open_id = page.evaluate("() => __winnow.S.sourceId")

    lookup_def = None
    try:
        page.evaluate("() => __winnow.openDerivedColumnModal('Host')")
        page.wait_for_selector("#modal:not([hidden])")
        page.locator("#modalBody select").nth(1).select_option(label="Look up from another table")
        page.wait_for_timeout(200)

        # The table picker offers the other real tables — never the open one.
        offered = page.evaluate("""() =>
          [...document.querySelectorAll('.derived-param select')[0].options].map((o) => o.value)""")
        assert str(open_id) not in offered
        assert str(ids["owners.csv"]) in offered and str(ids["sites.csv"]) in offered

        def key_cols():
            return page.evaluate("""() =>
              [...document.querySelectorAll('.derived-param select')[1].options].map((o) => o.value)""")

        # The key/value pickers list the CHOSEN table's columns, and re-list
        # when the table changes rather than keeping the previous table's.
        page.locator(".derived-param select").nth(0).select_option(str(ids["sites.csv"]))
        page.wait_for_timeout(200)
        assert key_cols() == ["Machine", "Site"]
        page.locator(".derived-param select").nth(0).select_option(str(ids["owners.csv"]))
        page.wait_for_timeout(200)
        assert key_cols() == ["Host", "Owner"]

        page.locator(".derived-param select").nth(1).select_option("Host")
        page.locator(".derived-param select").nth(2).select_option("Owner")
        page.wait_for_selector(".derived-preview-row")
        assert "alice" in page.locator(".derived-preview-row").first.inner_text()  # row 1's Host is H0

        page.locator(".derived-name").fill("Owner")
        page.locator("#modalBody .btn", has_text="Add column").first.click()
        page.wait_for_function(
            "() => __winnow.S.columns.some((c) => c.name === 'Owner' && c.derived)", timeout=15000)
        page.wait_for_function("""() => {
          const i = __winnow.S.columns.findIndex((c) => c.name === 'Owner');
          const r = __winnow.rowAt(0);
          return r && r.cells[i] === 'alice';
        }""", timeout=15000)
        defs = page.evaluate("""() => fetch('/api/derived?source_id=' + __winnow.S.sourceId,
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
        lookup_def = next(d for d in defs if d["name"] == "Owner")
    finally:
        # Leave the shared session case the way the other tests expect it.
        if lookup_def:
            page.evaluate("""(id) => fetch('/api/derived/' + id, { method: 'DELETE',
              headers: { 'X-Timeline-Lite-Client': '1' } })""", lookup_def["id"])
        for name in ("owners.csv", "sites.csv"):
            page.evaluate("""(id) => fetch('/api/source/' + id, { method: 'DELETE',
              headers: { 'X-Timeline-Lite-Client': '1' } })""", ids[name])
        page.evaluate("() => __winnow.loadSources()")
        page.evaluate("(id) => __winnow.openSource(id)", open_id)
