"""Across cases has no header button while the feature is reconsidered —
but the modal itself is still there for whoever picks it back up."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_no_button_but_the_modal_is_still_reachable(page):
    assert page.locator("#btnAcrossCases").count() == 0
    labels = page.locator(".bar-actions .btn").all_inner_texts()
    assert not any("Across cases" in l for l in labels), labels
    assert page.evaluate("() => typeof __winnow.openMultiCase") == "function"
