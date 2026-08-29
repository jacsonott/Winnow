"""The derived-column modal's Format list is grouped by kind — timestamps,
extraction, comparisons — instead of one flat thirteen-format wall."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_format_list_is_grouped(page):
    page.evaluate("() => __winnow.openDerivedColumnModal('CommandLine')")
    page.wait_for_selector("#modal:not([hidden])")
    groups = page.evaluate("""() => {
      const sel = document.querySelectorAll('#modalBody select')[1];
      return [...sel.querySelectorAll('optgroup')].map((g) =>
        [g.label, [...g.querySelectorAll('option')].length]);
    }""")
    labels = [g[0] for g in groups]
    assert labels == ["Timestamps", "Extract part of a value", "Join from another table", "Comparisons"]
    counts = dict(groups)
    assert counts["Extract part of a value"] == 3  # JSON, XML, regex
    assert counts["Timestamps"] >= 10
    # every op is reachable — nothing fell outside the groups
    total = page.evaluate("""() => document.querySelectorAll('#modalBody select')[1].options.length""")
    assert total == sum(c for _, c in groups)
    # picking from a group still works end to end
    page.locator("#modalBody select").nth(1).select_option(label="Regex capture")
    page.wait_for_timeout(200)
    assert page.locator(".derived-name").input_value() == "CommandLine (extract)"
    page.keyboard.press("Escape")
