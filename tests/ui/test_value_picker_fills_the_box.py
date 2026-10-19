"""The value picker writes what it applies into the column's filter box —
every time, not just when the values happen to be plain.

It used to park some selections in the guided filter tree instead, under
Filters ▾, and leave the box empty: "(empty)" ticked beside real values
(which is every Excel-style "untick the noise" on a column with blanks), a
value containing `|`, a value with a space at either end. So the same
gesture filled the box on one column and not on the next. The box quotes
those values now, and these tests hold the picker to the box, and the box
to the rows it says.

A list whose first value starts with an operator character, or a list of
paths that starts and ends with `/`, was worse: it did land in the box,
and the box read it as a comparison or a regex — rows nobody picked.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

# Counts chosen so the picker (most common first) lists each column in a
# known order: the order is what decides which value leads the list.
ROWS = (
    [["", "<unknown>", "/usr/bin/", "a|b"]] * 5
    + [["svchost.exe", "mid", "/tmp/", "SYSTEM"]] * 4
    + [["lsass.exe", " lead", "/etc/", "bob"]] * 3
    + [["cmd.exe", "trail ", "/var/log/", "carol"]] * 2
)


@pytest.fixture
def picks(page, server_post, ui_csv, until):
    p = Path(ui_csv).parent / "picks.csv"
    p.write_text("Proc,Pad,Path,User\n" + "".join(",".join(r) + "\n" for r in ROWS))
    server_post("/api/ingest/jobs/path", {"path": str(p), "build_fts": False})
    until(page, lambda: page.evaluate("""() => __winnow.loadSources(undefined, { navigate: false }).then(() =>
            __winnow.S.sources.some((s) => s.name === 'picks.csv' && s.row_count === 14))"""),
          "picks.csv to finish importing", timeout=20)
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'picks.csv').id")
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view", arg=sid)
    yield sid
    page.evaluate("(id) => __winnow.api(`/api/source/${id}`, { method: 'DELETE' }).then(() => __winnow.loadSources())", sid)


def _open_picker(page, col):
    page.locator(f'.fcell-pick[data-col="{col}"]').click()
    page.wait_for_selector(".value-picker .vp-row")


def _row(page, shown):
    """A picker row by its value, matched whole: `has_text` is a substring
    match, and "a|b" or "/tmp/" would find more than one row by it."""
    return page.locator(".value-picker .vp-row").filter(has=page.get_by_text(shown, exact=True))


def _apply(page, view_rebuilt):
    with view_rebuilt(page):
        page.locator(".value-picker .vp-actions .btn:not(.ghost)").click()


def _box(page, col):
    return page.locator(f'.fcell input[data-col="{col}"]').input_value()


def _state(page):
    return page.evaluate("""() => ({ rows: __winnow.S.view.row_count,
      tree: (__winnow.S.filterTree.children || []).length })""")


def test_unticking_a_value_on_a_column_with_blanks_fills_the_box(page, picks, view_rebuilt):
    """The reported case: everything starts ticked, (empty) included, and
    unticking one value is the whole gesture."""
    _open_picker(page, "Proc")
    _row(page, "svchost.exe").locator("input").uncheck()
    _apply(page, view_rebuilt)
    assert _box(page, "Proc") == '""|lsass.exe|cmd.exe'
    assert _state(page) == {"rows": 10, "tree": 0}


@pytest.mark.parametrize("col, keep, box, rows", [
    # A value holding the list's own separator.
    ("User", ["a|b", "bob"], '"a|b"|bob', 8),
    # Spaces at the edges, which the box would otherwise trim away.
    ("Pad", [" lead", "trail "], '" lead"|"trail "', 5),
])
def test_values_the_bare_box_cannot_spell_are_quoted_into_it(page, picks, view_rebuilt, col, keep, box, rows):
    _open_picker(page, col)
    page.locator(".value-picker .vp-actions .btn", has_text="None").click()
    for v in keep:
        _row(page, v).locator("input").check()
    _apply(page, view_rebuilt)
    assert _box(page, col) == box
    assert _state(page) == {"rows": rows, "tree": 0}


@pytest.mark.parametrize("col, untick, box, rows", [
    # Led by `<`: was read as "less than 'unknown>|mid|…'".
    ("Pad", "trail ", '"<unknown>"|mid|" lead"', 12),
    # Starts and ends with `/`: was read as a regex.
    ("Path", "/var/log/", '"/usr/bin/"|"/tmp/"|"/etc/"', 12),
])
def test_a_list_led_by_an_operator_character_means_the_values(page, picks, view_rebuilt, col, untick, box, rows):
    _open_picker(page, col)
    _row(page, untick).locator("input").uncheck()
    _apply(page, view_rebuilt)
    assert _box(page, col) == box
    assert _state(page)["rows"] == rows


def test_reopening_the_picker_ticks_what_the_box_says(page, picks, view_rebuilt):
    """The box is now the only record of the selection, so it has to read
    back as exactly the values that were ticked — quotes and (empty) too."""
    _open_picker(page, "User")
    page.locator(".value-picker .vp-actions .btn", has_text="None").click()
    for v in ("a|b", "carol"):
        _row(page, v).locator("input").check()
    _apply(page, view_rebuilt)
    _open_picker(page, "User")
    ticked = page.evaluate("""() => [...document.querySelectorAll('.value-picker .vp-row')]
      .filter((r) => r.querySelector('input').checked)
      .map((r) => r.querySelector('.vp-value').textContent)""")
    assert sorted(ticked) == ["a|b", "carol"]


def test_filtering_to_a_padded_cell_value_writes_it_quoted(page, picks, view_rebuilt):
    """`f` and the row menu go through the same writer; a padded value used
    to stop at a toast sending the analyst to the picker."""
    with view_rebuilt(page):
        page.evaluate("() => __winnow.filterByValue('Pad', ' lead')")
    assert _box(page, "Pad") == '=" lead"'
    assert _state(page)["rows"] == 3


def test_the_filter_builder_keeps_the_empty_value_it_absorbs(page, picks, view_rebuilt):
    """Opening the builder moves the boxes into its tree. A list holding the
    empty value goes in as `in` OR `is empty`, which the builder can show;
    an `in` holding '' would lose it to the one-per-line box's first edit."""
    page.locator('.fcell input[data-col="Proc"]').fill('""|cmd.exe')
    page.wait_for_function("() => __winnow.S.view.row_count === 7")
    page.evaluate("() => __winnow.openFilterBuilder()")
    page.wait_for_selector("#modal:not([hidden]) .fb-sql")
    sql = page.locator("#modal .fb-sql").input_value()
    assert "IS NULL" in sql and "cmd.exe" in sql, sql
    assert _box(page, "Proc") == ""
    page.evaluate("() => __winnow.closeModal()")
