"""Grouping on a column puts it in the output.

"Group by Host + User" produced a table of a timestamp and a description,
and which pair a row belonged to was recoverable only from whatever the
description template happened to name. The commonest grouping there is
needed two more drags before it said anything, and forgetting them
produced a result that looked right and could not be read.

The rule is deliberately one-way, and these pin both halves of that: every
way of adding a group column also includes it, and nothing automatically
takes an included column away again.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


DRAG = """(args) => {
  const [srcSel, dstSel] = args;
  const src = document.querySelector(srcSel);
  const dst = document.querySelector(dstSel);
  const dt = new DataTransfer();
  src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
  dst.dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt, cancelable: true }));
  dst.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt, cancelable: true }));
  src.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
}"""


def _zone(pg, name):
    return pg.evaluate(
        "(z) => [...document.querySelectorAll(`[data-zone='${z}'] [data-field]`)]"
        ".map((c) => c.dataset.field)", name)


def _chip(field):
    return f"[data-field='{field}']"


def test_dragging_a_column_into_group_rows_on_includes_it(fl_sheet):
    fl_sheet.evaluate(DRAG, [_chip("Host"), "[data-zone='groupBy']"])
    fl_sheet.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='carry'] [data-field]\").length === 1",
        timeout=10_000)
    assert _zone(fl_sheet, "groupBy") == ["Host"]
    assert _zone(fl_sheet, "carry") == ["Host"]


def test_it_reaches_the_output_table(fl_sheet):
    """Not just the chip — the column has to actually come back in the
    result, which is the thing the analyst was missing."""
    fl_sheet.evaluate(DRAG, [_chip("Host"), "[data-zone='groupBy']"])
    fl_sheet.wait_for_function(
        "() => [...document.querySelectorAll('thead th')].map((h) => h.textContent)"
        ".join('|') === 'Timestamp|Host|Description'", timeout=10_000)


def test_a_second_group_column_is_included_too_in_order(fl_sheet):
    fl_sheet.evaluate(DRAG, [_chip("Host"), "[data-zone='groupBy']"])
    fl_sheet.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='carry'] [data-field]\").length === 1")
    fl_sheet.evaluate(DRAG, [_chip("EventId"), "[data-zone='groupBy']"])
    fl_sheet.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='carry'] [data-field]\").length === 2",
        timeout=10_000)
    assert _zone(fl_sheet, "groupBy") == ["Host", "EventId"]
    assert _zone(fl_sheet, "carry") == ["Host", "EventId"]


def test_a_column_already_included_is_not_added_twice(fl_sheet):
    fl_sheet.evaluate(DRAG, [_chip("Host"), "[data-zone='carry']"])
    fl_sheet.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='carry'] [data-field]\").length === 1")
    fl_sheet.evaluate(DRAG, [_chip("Host"), "[data-zone='groupBy']"])
    fl_sheet.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='groupBy'] [data-field]\").length === 1",
        timeout=10_000)
    assert _zone(fl_sheet, "carry") == ["Host"]


def test_ungrouping_leaves_the_column_in_the_output(fl_sheet):
    """One way only. Dropping a column the analyst can see is the worse
    half of being wrong, and Include columns is theirs to prune."""
    fl_sheet.evaluate(DRAG, [_chip("Host"), "[data-zone='groupBy']"])
    fl_sheet.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='carry'] [data-field]\").length === 1")
    fl_sheet.evaluate("""() => document.querySelector("[data-zone='groupBy'] [data-field] button").click()""")
    fl_sheet.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='groupBy'] [data-field]\").length === 0",
        timeout=10_000)
    assert _zone(fl_sheet, "carry") == ["Host"]


def test_dragging_a_chip_out_of_include_columns_is_a_move(fl_sheet):
    """The carve-out: a drag that came FROM carry is a move gesture, and
    bouncing the chip back where it started would read as the drag having
    failed."""
    fl_sheet.evaluate(DRAG, [_chip("Host"), "[data-zone='carry']"])
    fl_sheet.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='carry'] [data-field]\").length === 1")
    fl_sheet.evaluate("""() => {
      const src = document.querySelector("[data-zone='carry'] [data-field='Host']");
      const dst = document.querySelector("[data-zone='groupBy']");
      const dt = new DataTransfer();
      src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
      dst.dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt, cancelable: true }));
      dst.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt, cancelable: true }));
      src.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
    }""")
    fl_sheet.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='groupBy'] [data-field]\").length === 1",
        timeout=10_000)
    assert _zone(fl_sheet, "groupBy") == ["Host"]
    assert _zone(fl_sheet, "carry") == []


def test_the_click_to_place_path_includes_it_too(fl_sheet):
    """Clicking a field offers the same placements — the keyboard and
    trackpad route through the same addField, and must not be a second
    behaviour. (The placements are a menu under the field now, not a
    "Place Host" dialog; tests/ui/test_field_place_menu.py is about the
    menu itself.)"""
    fl_sheet.evaluate("""() => document.querySelector("[data-field='Host']").click()""")
    fl_sheet.wait_for_selector(".menu .menu-item", timeout=10_000)
    fl_sheet.locator(".menu .menu-item", has_text="Group rows on").click()
    fl_sheet.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='carry'] [data-field]\").length === 1",
        timeout=10_000)
    assert _zone(fl_sheet, "groupBy") == ["Host"]
    assert _zone(fl_sheet, "carry") == ["Host"]
