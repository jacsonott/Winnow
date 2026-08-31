"""The new-extension launch prompt: the first boot after the catalogue
grows an extension asks once whether Winnow should be its default —
only on a machine that uses associations, and never again for an
extension once any answer (including Not now) is recorded."""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _post(server, route, body):
    req = urllib.request.Request(
        server.rstrip("/") + route, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


def _fresh(browser, server):
    ctx = browser.new_context(viewport={"width": 1300, "height": 800})
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.setItem('winnow.appearance', JSON.stringify({ splash: false }))")
    pg = ctx.new_page()
    pg.goto(server, wait_until="networkidle")
    pg.wait_for_selector(".row")
    return ctx, pg


def test_no_prompt_on_a_machine_that_uses_no_associations(browser, server):
    ctx, pg = _fresh(browser, server)
    try:
        pg.wait_for_timeout(600)
        assert pg.evaluate("() => document.getElementById('modal').hidden")
    finally:
        ctx.close()


def test_prompt_fires_once_and_any_answer_is_final(browser, server):
    _post(server, "/api/assoc/register", {"exts": [".csv"]})   # this machine uses associations
    try:
        ctx, pg = _fresh(browser, server)
        try:
            pg.wait_for_selector("#modal:not([hidden])", timeout=10_000)
            assert "new file types" in pg.locator("#modalTitle").inner_text().lower()
            body = pg.locator("#modalBody").inner_text()
            assert ".tsv" in body and ".db-winnow" in body
            pg.locator("#modal button", has_text="Open With only").click()
            pg.wait_for_selector("#modal[hidden]", state="attached")
            types = pg.evaluate("""() => fetch('/api/assoc/types',
              { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
            by = {t["ext"]: t for t in types["types"]}
            assert by[".tsv"]["registered"] is True
            assert all(t["prompted"] for t in types["types"] if t["default_ok"])
        finally:
            ctx.close()

        # Answered: a fresh boot must stay quiet.
        ctx2, pg2 = _fresh(browser, server)
        try:
            pg2.wait_for_timeout(700)
            assert pg2.evaluate("() => document.getElementById('modal').hidden")
        finally:
            ctx2.close()
    finally:
        # Leave the shared server as found (prompted entries stay — they
        # are what keeps this prompt out of every other test).
        for e in (".csv", ".tsv", ".jsonl", ".ndjson", ".db-winnow"):
            try: _post(server, "/api/assoc/unregister", {"exts": [e]})
            except Exception: pass
