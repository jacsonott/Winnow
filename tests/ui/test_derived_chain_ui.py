"""Chained derivation through the modal: a derived column offered (and
marked) as the input for another derived column."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _wait_ready(page, name, timeout_s=15):
    import time as _time

    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        ok = page.evaluate("""(n) => fetch('/api/derived?source_id=1',
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())
          .then((defs) => defs.some((d) => d.name === n && d.status === 'ready'))""", name)
        if ok:
            return
        _time.sleep(0.2)
    raise AssertionError(f"{name} never finished building")


def test_modal_offers_derived_inputs_and_builds_a_chain(page):
    rec = page.evaluate("""() => fetch('/api/derived', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_id: 1, name: 'ChainParent', input_column: 'CommandLine',
                             op_id: 'regex_extract', params: { pattern: '\\\\\\\\([\\\\w.]+\\\\.exe)' } }) })
      .then((r) => r.json())""")
    child = None
    try:
        # Poll from Python: page.evaluate awaits promises, wait_for_function
        # does NOT — a promise-returning predicate is truthy immediately.
        _wait_ready(page, "ChainParent")
        page.evaluate("() => __winnow.loadSources()")
        page.evaluate("() => __winnow.openSource(__winnow.S.sourceId)")  # the modal reads the OPEN table's columns
        page.wait_for_function(
            "() => __winnow.S.columns.some((c) => c.name === 'ChainParent' && c.derived_status === 'ready')")

        page.evaluate("() => __winnow.openDerivedColumnModal('ChainParent')")
        page.wait_for_selector("#modal:not([hidden])")
        # the parse-column list offers the derived parent, marked
        opts = page.evaluate("""() => [...document.querySelectorAll('#modalBody select')[0].options]
          .map((o) => [o.value, o.textContent])""")
        assert ["ChainParent", "ChainParent · derived"] in opts

        page.locator("#modalBody select").nth(1).select_option(label="Extract part of a value")  # type
        page.locator("#modalBody select").nth(2).select_option(label="Regex capture")            # operation
        page.locator(".derived-param input").first.fill(r"^(\w+)")
        page.wait_for_selector(".derived-preview-row")
        assert "powershell" in page.locator(".derived-preview-row").first.inner_text()
        page.locator(".derived-name").fill("ChainChild")
        page.locator("#modalBody .btn", has_text="Add column").first.click()
        page.wait_for_function(
            "() => __winnow.S.columns.some((c) => c.name === 'ChainChild' && c.derived)", timeout=15000)
        child = page.evaluate("""() => fetch('/api/derived?source_id=1',
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())
          .then((defs) => (defs.find((d) => d.name === 'ChainChild') || {}).id)""")
    finally:
        # children first — the parent refuses to go while one exists
        for did in [child, rec["definition"]["id"]]:
            if did:
                page.evaluate("""(id) => fetch('/api/derived/' + id, { method: 'DELETE',
                  headers: { 'X-Timeline-Lite-Client': '1' } })""", did)
        page.evaluate("() => __winnow.loadSources()")
