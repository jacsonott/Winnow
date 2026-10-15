"""A log with an extension nothing claims, end to end through the import
queue: queued as raw text, imported one line per row into a Message
column. Also that the CSV preview's Lines option is there to force it.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture
def log_file(ui_csv) -> Path:
    p = Path(ui_csv).parent / "hostd.log.1"
    p.write_text("2026-03-14 08:00:01 hostd: start, with, commas\n\n2026-03-14 08:00:02 hostd: \"quote\n2026-03-14 08:00:03 hostd: end\n")
    return p


def test_log_queues_as_text_and_imports_one_line_per_row(page, log_file):
    try:
        page.evaluate("(p) => { __winnow.S.importQueue = []; __winnow.queuePaths([{ path: p, name: 'hostd.log.1' }]); __winnow.openImportModal(); }", str(log_file))
        page.wait_for_selector("#modal:not([hidden])")
        row = page.locator("#modalBody .session-row", has_text="hostd.log.1")
        assert "text · one line per row" in row.inner_text()
        # The preview opens on the Lines option, header control disabled
        row.locator("button", has_text="Preview").click()
        page.wait_for_selector("#modalBody select")
        assert page.locator("#modalBody select").first.input_value() == "lines"
        assert page.locator("#modalBody input[type=checkbox]").is_disabled()
        page.wait_for_selector("#modalBody .preview-tbl")
        assert "One line per row" in page.locator("#modalBody .note-status").first.inner_text()
        page.locator("#modalBody button", has_text="Use these settings").click()
        page.wait_for_selector("#modalBody .session-row")
        page.locator("#modalBody button", has_text="Import all queued").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'hostd.log.1' && s.row_count === 4)", timeout=20_000)
        sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'hostd.log.1').id")
        page.evaluate("(id) => __winnow.openSource(id)", sid)
        page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.S.view && __winnow.S.view.row_count === 4", arg=sid)
        cols = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'hostd.log.1').columns.map((c) => c.name)")
        assert cols == ["Message"]
        first = page.locator("#body .row").first
        assert "start, with, commas" in first.inner_text()
    finally:
        # By name rather than by the id the body found: a test that fails
        # before it gets that far has still queued the import. This used to
        # run on success only, through /api/sources/{id}, a route that does
        # not exist, with the 404 swallowed, so the table was never removed
        # from the case later modules share (#227).
        page.evaluate("""async () => {
          const ss = await __winnow.api('/api/sources');
          for (const s of ss.filter((s) => s.name === 'hostd.log.1'))
            await __winnow.api('/api/source/' + s.id, { method: 'DELETE' });
          await __winnow.loadSources();
        }""")
