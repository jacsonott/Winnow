"""The new-extension launch prompt: the first boot after the catalogue
grows an extension asks once whether Winnow should be its default —
only on a machine that uses associations, and never again for an
extension once any answer (including Not now) is recorded."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _fresh(own_page):
    return own_page(viewport=(1300, 800))


def test_no_prompt_on_a_machine_that_uses_no_associations(own_page):
    with _fresh(own_page) as pg:
        pg.wait_for_timeout(600)   # past the round trips the prompt would follow
        assert pg.evaluate("() => document.getElementById('modal').hidden")


def test_prompt_fires_once_and_any_answer_is_final(own_page, server_post):
    server_post("/api/assoc/register", {"exts": [".csv"]})   # this machine uses associations
    try:
        with _fresh(own_page) as pg:
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

        # Answered: a fresh boot must stay quiet.
        with _fresh(own_page) as pg2:
            pg2.wait_for_timeout(700)   # past the round trips the prompt would follow
            assert pg2.evaluate("() => document.getElementById('modal').hidden")
    finally:
        # Leave the shared server as found (prompted entries stay — they
        # are what keeps this prompt out of every other test).
        for e in (".csv", ".tsv", ".jsonl", ".ndjson", ".db-winnow"):
            try: server_post("/api/assoc/unregister", {"exts": [e]})
            except Exception: pass
