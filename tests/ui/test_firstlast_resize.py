"""First/Last layout: the rail resizes from its edge (remembered on this
machine), and the preview's columns resize from their header edges (per
sheet, surviving a re-render)."""

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


def _drag(pg, locator, dx):
    box = locator.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    pg.mouse.move(x, y)
    pg.mouse.down()
    pg.mouse.move(x + dx / 2, y, steps=4)
    pg.mouse.move(x + dx, y, steps=4)
    pg.mouse.up()


def test_the_rail_resizes_from_its_edge_and_is_remembered(fl_page):
    pg = fl_page
    rail = pg.locator(".fl-rail")
    w0 = rail.bounding_box()["width"]
    _drag(pg, pg.locator(".fl-rail-resize"), 120)
    w1 = rail.bounding_box()["width"]
    assert abs(w1 - (w0 + 120)) < 4, (w0, w1)
    assert int(pg.evaluate("() => localStorage.getItem('winnow.firstlast.rail')")) == round(w1)
    # Bounded: it can't be dragged to nothing.
    _drag(pg, pg.locator(".fl-rail-resize"), -900)
    assert rail.bounding_box()["width"] >= 200
    # Double-click puts it back.
    pg.locator(".fl-rail-resize").dblclick()
    assert abs(rail.bounding_box()["width"] - 280) < 2
    assert pg.evaluate("() => localStorage.getItem('winnow.firstlast.rail')") == "280"


def test_preview_columns_resize_from_the_header_edge_and_survive_a_rerun(fl_page):
    pg = fl_page
    pg.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    pg.wait_for_selector("table tbody tr", timeout=10_000)
    th = pg.locator("table thead th").first
    w0 = th.bounding_box()["width"]
    _drag(pg, th.locator(".fl-col-resize"), 90)
    w1 = th.bounding_box()["width"]
    assert abs(w1 - (w0 + 90)) < 4, (w0, w1)
    # The cells below follow the header.
    assert abs(pg.locator("table tbody tr").first.locator("td").first.bounding_box()["width"] - w1) < 4
    # A re-render (the preview re-runs) keeps the width — it's sheet state.
    pg.locator("button.fl-refresh").click()
    pg.wait_for_function("() => ![...document.querySelectorAll('.note-status')].some((n) => n.textContent === 'Previewing…')",
                         timeout=10_000)
    pg.wait_for_selector("table tbody tr", timeout=10_000)
    assert abs(pg.locator("table thead th").first.bounding_box()["width"] - w1) < 4
    # Resizing never started a header reorder drag (still one grouping, same order).
    assert pg.locator("table thead th").first.inner_text().strip() == "TIMESTAMP"
    # Double-click the grip: back to fit.
    pg.locator("table thead th").first.locator(".fl-col-resize").dblclick()
    assert pg.locator("table thead th").first.bounding_box()["width"] < w1 - 40
