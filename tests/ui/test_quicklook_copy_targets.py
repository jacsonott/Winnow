"""The quick look's "Add these tables to a case" list offers only cases
whose file is there.

It filtered on `c.missing`, but /api/cases marks a vanished file with
`exists: false` and has no `missing` key — so every registered case was
offered, and picking one whose file had gone only failed after the copy
was asked for."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.ui


def test_a_case_whose_file_is_gone_is_not_offered_as_a_copy_target(page, api, tmp_path):
    here = api("/api/cases", "POST", {"path": str(tmp_path / "still-here.db"), "name": "UI copy target here"})
    gone = api("/api/cases", "POST", {"path": str(tmp_path / "gone.db"), "name": "UI copy target gone"})
    try:
        for suffix in ("", "-wal", "-shm", ".winnow-lock"):
            if os.path.exists(gone["path"] + suffix):
                os.remove(gone["path"] + suffix)
        assert next(c for c in api("/api/cases") if c["id"] == gone["id"])["exists"] is False

        page.evaluate("() => __winnow.tempCopyModal()")
        page.locator("#modalBody .session-row", has_text="UI copy target here").wait_for()
        names = page.locator("#modalBody .session-row .session-name").all_inner_texts()
        assert "UI copy target gone" not in names
        page.keyboard.press("Escape")
    finally:
        api(f"/api/cases/{here['id']}", "DELETE")
        api(f"/api/cases/{gone['id']}", "DELETE")
