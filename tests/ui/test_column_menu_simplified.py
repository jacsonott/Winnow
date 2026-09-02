"""The column-header right-click menu is three verbs — Stack, Derive, and
Flatten only when the column holds JSON/XML — with display formats kept
for datetime columns. Driven against a table with a real JSON column."""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _post(server, route, body):
    req = urllib.request.Request(
        server.rstrip("/") + route, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def test_flatten_appears_only_for_document_columns(page, server, tmp_path):
    f = tmp_path / "events.csv"
    f.write_text("When,Payload,Note\n"
                 "2026-03-14 08:00:00,\"{\"\"user\"\": \"\"a\"\"}\",plain\n"
                 "2026-03-14 08:00:01,\"{\"\"user\"\": \"\"b\"\"}\",plain\n")
    _post(server, "/api/ingest/jobs/path", {"path": str(f), "name": "events.csv", "kind": "csv"})
    page.wait_for_function(
        "() => __winnow.loadSources().then(() => __winnow.S.sources.some((s) => s.name === 'events.csv'))",
        timeout=15_000)
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'events.csv').id")
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_selector('.hcell[data-col="Payload"]')
    page.wait_for_selector(".row")
    try:
        page.locator('.hcell[data-col="Payload"]').click(button="right")
        page.wait_for_selector(".menu")
        items = page.locator(".menu .menu-item").all_inner_texts()
        assert any("Flatten" in i for i in items), items
        assert any("Derive" in i for i in items) and any("Stack" in i for i in items)
        page.keyboard.press("Escape")
        page.locator('.hcell[data-col="Note"]').click(button="right")
        page.wait_for_selector(".menu")
        items = page.locator(".menu .menu-item").all_inner_texts()
        assert not any("Flatten" in i for i in items), items
        page.keyboard.press("Escape")
    finally:
        page.evaluate("""async (id) => {
          const h = { 'X-Timeline-Lite-Client': '1' };
          await fetch('/api/source/' + id, { method: 'DELETE', headers: h });
          __winnow.S.viewCache.delete(id);
          if (__winnow.S.sourceId === id) __winnow.S.sourceId = null;
          await __winnow.loadSources();
          const first = __winnow.S.sources.find((s) => !s.is_merge);
          if (first) __winnow.openSource(first.id);
        }""", sid)
        page.wait_for_selector(".row")
