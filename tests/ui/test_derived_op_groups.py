"""The Add derived column dialog's side list: what to make, listed down
the left by group, searchable, with pinned favourites on top.

It replaced a Type select and an Operation select whose choices rebuilt
the rows under them, and a dialog centred on screen that resized with
them: every change of type, operation or format slid its header up or
down. The test that matters most here asserts that symptom — nothing in
the frame moves while the analyst goes through every entry — rather than
the fixed heights that fixed it."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

GROUPS = ["Time", "Extract", "Decode", "Combine", "Join"]


def _open(page, format_detected, column="CommandLine"):
    page.evaluate("(c) => __winnow.openDerivedColumnModal(c)", column)
    page.wait_for_selector("#modal:not([hidden]) .derived-rail-item")
    format_detected()


def _rail(page):
    """The list as it reads: [heading, [entry labels]] per section."""
    return page.evaluate("""() => {
      const out = [];
      for (const n of document.querySelectorAll('#modalBody .derived-rail-list > *')) {
        if (n.classList.contains('derived-rail-group')) out.push([n.textContent, []]);
        else if (n.classList.contains('derived-rail-row'))
          out[out.length - 1][1].push(n.querySelector('.derived-rail-label').textContent);
      }
      return out;
    }""")


def _selected(page):
    return page.locator('#modalBody .derived-rail-item[aria-selected="true"] .derived-rail-label').inner_text()


@pytest.fixture
def no_pins(page):
    """Pins live in localStorage, which the shared page keeps between
    tests: start without any and leave none behind."""
    clear = """() => { try { const all = JSON.parse(localStorage.getItem('winnow.menupins') || '{}');
      delete all.derive; localStorage.setItem('winnow.menupins', JSON.stringify(all)); } catch {} }"""
    page.evaluate(clear)
    yield
    page.evaluate("() => __winnow.closeModal()")
    page.evaluate(clear)


def test_every_kind_of_column_is_one_click_away(page, format_detected, derive_pick, no_pins):
    _open(page, format_detected)
    rail = _rail(page)
    assert [g for g, _ in rail] == GROUPS, rail
    # The timestamp formats are one entry with a Format list inside it;
    # everything else is an entry of its own.
    assert rail[0][1] == ["Parse a timestamp", "Duration between two columns"], rail
    assert rail[1][1] == ["JSON field", "XML field", "Regex capture"], rail
    assert rail[2][1] == ["URL decode", "Base64 decode"], rail
    assert rail[3][1] == ["First non-empty value", "Row as JSON"], rail
    assert rail[4][1] == ["Look up from another table"], rail
    # A new column starts on parsing a timestamp, and its Format list
    # holds the formats and nothing else.
    assert _selected(page) == "Parse a timestamp"
    formats = page.evaluate(
        "() => [...document.querySelector('#modalBody select[data-role=format]').options].map((o) => o.textContent)")
    assert len(formats) >= 10 and "Windows FILETIME" in formats, formats
    assert not {"Regex capture", "Duration between two columns", "URL decode"} & set(formats), formats

    # The suggested name follows what is being made.
    derive_pick("Regex capture")
    assert page.locator(".derived-name").input_value() == "CommandLine (extract)"
    assert page.locator('#modalBody select[data-role="format"]').count() == 0   # a timestamp's control only
    derive_pick("Base64 decode")
    assert page.locator(".derived-name").input_value() == "CommandLine (decoded)"
    derive_pick("Duration between two columns")
    assert page.locator(".derived-name").input_value() == "CommandLine elapsed"


def test_nothing_in_the_dialog_moves_as_the_choices_change(page, format_detected, derive_pick, no_pins):
    """The complaint, as it was made: the dialog's size and position
    changed as the type and fields changed. Every entry, a few formats
    and a regex whose named groups appear — the card, the list, the
    column picker, the name field, the preview and the buttons stay
    exactly where they are."""
    _open(page, format_detected)
    where = """() => {
      const r = (sel) => { const n = document.querySelector(sel); if (!n) return null;
        const b = n.getBoundingClientRect(); return [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; };
      return { card: r('.modal-card'), list: r('#modalBody .derived-rail-list'),
               column: r('#modalBody select[data-role=column]'), name: r('#modalBody .derived-name-row'),
               preview: r('#modalBody .derived-preview'),
               // its left and top: the label says how many columns, so its width may change
               go: r('#modalBody .row-actions .btn').slice(0, 2) };
    }"""
    before = page.evaluate(where)
    assert all(before.values()), before
    labels = [e for _, entries in _rail(page) for e in entries]
    for label in labels + ["Parse a timestamp"]:
        derive_pick(label)
        assert page.evaluate(where) == before, f"the dialog moved on picking {label}"
    for fmt in ("BSD syslog (Mmm dd hh:mm:ss)", "Windows FILETIME", "ISO 8601"):
        derive_pick("Parse a timestamp", fmt)
        assert page.evaluate(where) == before, f"the dialog moved on picking {fmt}"

    # A pattern that names its groups grows an offer of a column for each,
    # and the single name field steps aside for a line saying why.
    derive_pick("Regex capture")
    page.locator(".derived-param", has_text="Regex").locator("input").first.fill(
        r"^(?P<exe>\S+) -Enc (?P<payload>\S+)")
    page.wait_for_selector("#modalBody .derived-group-row", timeout=15_000)
    assert page.locator("#modalBody .derived-name-aside").is_visible()
    assert page.evaluate(where) == before, "the dialog moved when the named groups appeared"


def test_the_search_box_narrows_the_list(page, format_detected, no_pins):
    _open(page, format_detected)
    search = page.locator("#modalBody .derived-search")
    # The dialog opens with the caret in it, so typing searches.
    assert page.evaluate("() => document.activeElement.classList.contains('derived-search')")
    page.keyboard.type("base64")
    assert _rail(page) == [["Decode", ["Base64 decode"]]]

    # A timestamp format is found through the timestamp entry, which says
    # which one matched — and picking the hit picks that format.
    search.fill("filetime")
    assert _rail(page) == [["Time", ["Parse a timestamp"]]]
    hit = page.locator("#modalBody .derived-rail-item")
    assert hit.locator(".derived-rail-hint").inner_text() == "Windows FILETIME"
    hit.click()
    assert page.locator('#modalBody select[data-role="format"]').input_value() == "windows_filetime"

    search.fill("qqqq")   # not "zzzz": the Apache log format describes its offset as +zzzz
    assert page.locator("#modalBody .derived-rail-empty").inner_text() == "Nothing matches “qqqq”."

    # Escape clears the search first; only an empty search lets Escape
    # close the dialog.
    search.press("Escape")
    assert search.input_value() == ""
    assert page.locator("#modal").is_visible()
    assert [g for g, _ in _rail(page)] == GROUPS


def test_the_arrow_keys_walk_the_list(page, format_detected, no_pins):
    _open(page, format_detected)
    search = page.locator("#modalBody .derived-search")
    search.fill("decode")
    search.press("ArrowDown")
    assert _selected(page) == "URL decode"
    page.keyboard.press("ArrowDown")
    assert _selected(page) == "Base64 decode"
    page.keyboard.press("ArrowUp")
    page.keyboard.press("ArrowUp")    # from the top of the list, back to the search box
    assert page.evaluate("() => document.activeElement.classList.contains('derived-search')")
    search.fill("json")
    search.press("Enter")             # Enter takes the first match
    assert _selected(page) == "JSON field"


def test_a_pinned_entry_sits_on_top_and_stays_there(page, format_detected, derive_pick, no_pins):
    _open(page, format_detected)
    row = page.locator("#modalBody .derived-rail-row", has_text="Regex capture")
    row.hover()
    row.locator(".derived-rail-pin").click()
    rail = _rail(page)
    assert rail[0] == ["Pinned", ["Regex capture"]], rail
    # Moved, not copied: it leaves its own group.
    assert dict(rail)["Extract"] == ["JSON field", "XML field"], rail
    page.locator("#modalBody .derived-rail-row", has_text="URL decode").locator(".derived-rail-pin").click()
    assert _rail(page)[0] == ["Pinned", ["Regex capture", "URL decode"]]   # in the order pinned

    # Kept for the next time the dialog opens — on this machine.
    page.evaluate("() => __winnow.closeModal()")
    _open(page, format_detected)
    assert _rail(page)[0] == ["Pinned", ["Regex capture", "URL decode"]]
    # A pinned entry is found by the search like any other.
    page.locator("#modalBody .derived-search").fill("regex")
    assert _rail(page) == [["Pinned", ["Regex capture"]]]
    page.locator("#modalBody .derived-search").fill("")

    # ✕ puts it back where it came from.
    pinned = page.locator("#modalBody .derived-rail-row", has_text="Regex capture")
    assert pinned.locator(".derived-rail-pin").inner_text() == "✕"
    pinned.locator(".derived-rail-pin").click()
    rail = _rail(page)
    assert rail[0] == ["Pinned", ["URL decode"]], rail
    assert dict(rail)["Extract"] == ["JSON field", "XML field", "Regex capture"], rail
