"""Copying rows or a cell puts the DISPLAYED value on the clipboard — a
datetime column's chosen format applies to the copy exactly as it does to
the grid — instead of reverting to the stored text."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_copied_timestamps_keep_the_column_s_display_format(page, copied):
    raw = page.evaluate("() => __winnow.rowAt(0).cells[__winnow.S.columns.findIndex((c) => c.name === 'Timestamp')]")
    assert raw.startswith("2026-03-14 08:")
    # Switch the Timestamp column to US display and repaint.
    page.evaluate("""() => {
      __winnow.S.layout['Timestamp'] = Object.assign({}, __winnow.S.layout['Timestamp'] || {}, { tsFormat: 'us' });
      __winnow.render();
    }""")
    page.wait_for_function(
        "() => document.querySelector('.row .cell') && /^03\\/14\\/2026/.test(document.querySelector('.row .cell:nth-child(2), .row .cell').textContent) || true")
    with copied(page) as clip:
        page.evaluate("() => __winnow.copyRowsAsText([0], false)")
    assert "03/14/2026 08:" in clip.text, clip.text
    assert "2026-03-14" not in clip.text
    # Back to stored text → copy is the stored text again.
    page.evaluate("""() => {
      __winnow.S.layout['Timestamp'] = Object.assign({}, __winnow.S.layout['Timestamp'] || {}, { tsFormat: 'raw' });
      __winnow.render();
    }""")
    with copied(page) as clip:
        page.evaluate("() => __winnow.copyRowsAsText([0], false)")
    assert "2026-03-14 08:" in clip.text, clip.text
    page.evaluate("() => { delete __winnow.S.layout['Timestamp'].tsFormat; __winnow.render(); }")
