"""The derive modal's "a column per named group" offer.

Writing one regex and getting four columns out of it is the whole point
of the feature, and the part that can only be checked in a browser is the
offer itself: that the groups appear as the pattern is typed, that the
button stops saying "Add column" and says how many, and that the single
name field gets out of the way when it no longer applies.

Nothing is created here — the modal is closed before the button is
pressed — so the session case is left exactly as found.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# Against the fixture's CommandLine column: a path and the -Enc payload.
PATTERN = r"^(?P<exe>\S+) -Enc (?P<payload>\S+)"


def _open_regex_derive(page):
    page.evaluate("() => __winnow.openDerivedColumnModal('CommandLine')")
    page.wait_for_selector("#modal:not([hidden])")
    # Type → Extract part of a value, Operation → Regex capture.
    page.wait_for_function("() => document.querySelectorAll('#modalBody select').length >= 3")
    page.evaluate("""() => {
      const sels = [...document.querySelectorAll('#modalBody select')];
      const type = sels[0];
      type.value = [...type.options].find((o) => o.value.startsWith('Extract')).value;
      type.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_function("""() => [...document.querySelectorAll('#modalBody select')]
      .some((s) => [...s.options].some((o) => o.value === 'regex_extract'))""")
    page.evaluate("""() => {
      const op = [...document.querySelectorAll('#modalBody select')]
        .find((s) => [...s.options].some((o) => o.value === 'regex_extract'));
      op.value = 'regex_extract';
      op.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_selector("#modalBody .derived-params input", timeout=10_000)


def _type_pattern(page, pattern):
    page.evaluate("""(p) => {
      const box = [...document.querySelectorAll('#modalBody .derived-param')]
        .find((r) => r.textContent.startsWith('Regex'));
      const input = box.querySelector('input');
      input.value = p;
      input.dispatchEvent(new Event('input'));
    }""", pattern)


def test_a_named_pattern_offers_one_column_per_group(page):
    try:
        _open_regex_derive(page)
        _type_pattern(page, PATTERN)
        page.wait_for_selector("#modalBody .derived-group-row", timeout=15_000)
        keys = page.locator("#modalBody .derived-group-key").all_inner_texts()
        assert keys == ["exe", "payload"], keys
        # Each row shows what it would pull out, so the pattern can be
        # judged before a backfill rather than after one.
        samples = page.locator("#modalBody .derived-group-samples").first.inner_text()
        assert "powershell" in samples.lower(), samples
        # The names default to the group names, and are editable.
        names = page.evaluate("""() => [...document.querySelectorAll('#modalBody .derived-group-name')]
          .map((i) => i.value)""")
        assert names == ["exe", "payload"]
        # The button counts them, and the one-column name field steps aside.
        go = page.locator("#modalBody .row-actions .btn").first
        assert go.inner_text() == "Add 2 columns"
        assert page.locator("#modalBody .derived-name").is_hidden()
    finally:
        page.keyboard.press("Escape")


def test_unticking_the_groups_puts_the_single_column_back(page):
    try:
        _open_regex_derive(page)
        _type_pattern(page, PATTERN)
        page.wait_for_selector("#modalBody .derived-group-row", timeout=15_000)
        for cb in page.locator("#modalBody .derived-group-pick input").all():
            cb.uncheck()
        go = page.locator("#modalBody .row-actions .btn").first
        assert go.inner_text() == "Add column"
        assert page.locator("#modalBody .derived-name").is_visible()
    finally:
        page.keyboard.press("Escape")


def test_a_pattern_with_no_names_offers_nothing_and_says_nothing(page):
    """The old single-column path is untouched: an unnamed pattern gets
    the same modal it always had, with no empty section in it."""
    try:
        _open_regex_derive(page)
        _type_pattern(page, r"^(\S+) -Enc")
        page.wait_for_timeout(600)      # long enough for the probe to answer
        assert page.locator("#modalBody .derived-group-row").count() == 0
        assert page.locator("#modalBody .row-actions .btn").first.inner_text() == "Add column"
    finally:
        page.keyboard.press("Escape")


def test_a_name_the_table_already_uses_blocks_the_add_and_says_why(page):
    try:
        _open_regex_derive(page)
        _type_pattern(page, PATTERN)
        page.wait_for_selector("#modalBody .derived-group-row", timeout=15_000)
        page.evaluate("""() => {
          const input = document.querySelector('#modalBody .derived-group-name');
          input.value = 'Host';                 // a column the fixture table has
          input.dispatchEvent(new Event('input'));
        }""")
        go = page.locator("#modalBody .row-actions .btn").first
        page.wait_for_function("() => document.querySelector('#modalBody .row-actions .btn').disabled")
        assert "already has a column" in go.get_attribute("title")
    finally:
        page.keyboard.press("Escape")
