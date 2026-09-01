"""The derived-column modal picks TYPE first, then the specific operation —
so the extract / join / compare kinds are visible up front instead of buried
under a wall of timestamp formats in one grouped dropdown."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_type_first_picker(page):
    page.evaluate("() => __winnow.openDerivedColumnModal('CommandLine')")
    page.wait_for_selector("#modal:not([hidden])")
    # selects in the body: [Parse column, Type, Operation, …params]
    types = page.evaluate(
        "() => [...document.querySelectorAll('#modalBody select')[1].options].map((o) => o.value)")
    assert types == ["Timestamp", "Extract part of a value", "Join from another table", "Compare (elapsed time)"]

    # picking a type populates the Operation list with only that kind's ops
    page.locator("#modalBody select").nth(1).select_option(label="Extract part of a value")
    ops = page.evaluate(
        "() => [...document.querySelectorAll('#modalBody select')[2].options].map((o) => o.textContent)")
    assert len(ops) == 3 and any("Regex" in o for o in ops)   # JSON, XML, regex

    page.locator("#modalBody select").nth(1).select_option(label="Timestamp")
    ts = page.evaluate("() => document.querySelectorAll('#modalBody select')[2].options.length")
    assert ts >= 10

    # picking an operation still works end to end
    page.locator("#modalBody select").nth(1).select_option(label="Extract part of a value")
    page.locator("#modalBody select").nth(2).select_option(label="Regex capture")
    page.wait_for_timeout(200)
    assert page.locator(".derived-name").input_value() == "CommandLine (extract)"
    page.keyboard.press("Escape")
