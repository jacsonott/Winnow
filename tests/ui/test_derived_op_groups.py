"""The derived-column modal picks TYPE first, then the specific operation —
so the extract / join / compare kinds are visible up front instead of buried
under a wall of timestamp formats in one grouped dropdown."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_type_first_picker(page):
    page.evaluate("() => __winnow.openDerivedColumnModal('CommandLine')")
    page.wait_for_selector("#modal:not([hidden])")
    # What am I making, how, and from what: Type is the question the
    # analyst arrives with, the operation decides what the column list
    # means, and the column sits right above the parameters that read it.
    labels = page.evaluate(
        "() => [...document.querySelectorAll('#modalBody .derived-row-label')].map((n) => n.textContent)")
    assert labels[:3] == ["Type", "Operation", "Parse column"], labels
    types = page.evaluate(
        "() => [...document.querySelector('#modalBody select[data-role=type]').options].map((o) => o.value)")
    assert types == ["Timestamp", "Extract part of a value", "Join from another table",
                     "Compare (elapsed time)", "Combine columns"]

    # picking a type populates the Operation list with only that kind's ops
    page.locator("#modalBody select[data-role=type]").select_option(label="Extract part of a value")
    ops = page.evaluate(
        "() => [...document.querySelector('#modalBody select[data-role=op]').options].map((o) => o.textContent)")
    assert len(ops) == 3 and any("Regex" in o for o in ops)   # JSON, XML, regex

    page.locator("#modalBody select[data-role=type]").select_option(label="Timestamp")
    ts = page.evaluate("() => document.querySelector('#modalBody select[data-role=op]').options.length")
    assert ts >= 10

    # picking an operation still works end to end
    page.locator("#modalBody select[data-role=type]").select_option(label="Extract part of a value")
    page.locator("#modalBody select[data-role=op]").select_option(label="Regex capture")
    page.wait_for_timeout(200)
    assert page.locator(".derived-name").input_value() == "CommandLine (extract)"
    page.keyboard.press("Escape")
