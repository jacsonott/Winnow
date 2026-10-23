"""First/Last's description, from the field list: the chips under the box
are the functions rather than one per column, a field dragged into the box
lands as {Name} where it is dropped, and a field dropped on a function
fills it in. Plus the Count unique zone those functions sit beside."""

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

# A drop on the description box at a point: 'start', 'end', or a character
# index (just right of that character's left edge, measured the way a
# browser lays the box's text out).
DROP_AT = """(args) => {
  const [srcSel, dstSel, where] = args;
  const src = document.querySelector(srcSel);
  const dst = document.querySelector(dstSel);
  const r = dst.getBoundingClientRect();
  let x;
  if (where === 'start') x = r.left + 1;
  else if (where === 'end') x = r.right - 4;
  else {
    const cs = getComputedStyle(dst);
    const ctx = document.createElement('canvas').getContext('2d');
    ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    x = r.left + parseFloat(cs.borderLeftWidth) + parseFloat(cs.paddingLeft)
      + ctx.measureText(dst.value.slice(0, where)).width + 1;
  }
  const at = { clientX: x, clientY: r.top + r.height / 2 };
  const dt = new DataTransfer();
  src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
  dst.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt, ...at }));
  const kept = dst.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt, ...at }));
  src.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
  return kept;
}"""

TMPL = "input[style*='var(--mono)']"
FIELD = ".pluginview [data-field='{}']"   # the field list comes before the zones
DESCS = "() => [...document.querySelectorAll('table tbody tr td:last-child')].map((td) => td.textContent.trim())"


def _value(pg):
    return pg.evaluate(f"() => document.querySelector(\"{TMPL}\").value")


def _grouped(pg):
    pg.evaluate(DRAG, [FIELD.format("Host"), "[data-zone='groupBy']"])
    pg.wait_for_selector("table tbody tr", timeout=10_000)


def test_the_chips_are_the_functions_not_the_columns(fl_sheet):
    chips = fl_sheet.evaluate(
        f"() => [...document.querySelector(\"{TMPL}\").parentElement.querySelectorAll('button')]"
        ".map((b) => b.textContent)")
    assert chips == ["{which}", "{which:number}", "{count}", "{sum:…}", "{min:…}", "{max:…}",
                     "{values:…}", "{unique:…}"]
    # A plain one inserts itself at the caret.
    tmpl = fl_sheet.locator(TMPL).first
    tmpl.fill("of ")
    fl_sheet.locator("button", has_text="{count}").click()
    assert _value(fl_sheet) == "of {count}"


def test_a_field_dropped_in_the_box_lands_where_it_was_dropped(fl_sheet):
    _grouped(fl_sheet)
    tmpl = fl_sheet.locator(TMPL).first
    tmpl.fill("{which} of {count}")
    assert fl_sheet.evaluate(DROP_AT, [FIELD.format("EventId"), TMPL, "end"]) is False, \
        "the drop is the tab's: left to the browser, the dragged text goes in as is"
    assert _value(fl_sheet) == "{which} of {count}{EventId}"
    tmpl.fill("{which} of {count}")
    fl_sheet.evaluate(DROP_AT, [FIELD.format("EventId"), TMPL, "start"])
    assert _value(fl_sheet) == "{EventId}{which} of {count}"
    # Inside a placeholder is never a place for another: it goes after it.
    tmpl.fill("{which} of {count}")
    fl_sheet.evaluate(DROP_AT, [FIELD.format("CommandLine"), TMPL, 3])
    assert _value(fl_sheet) == "{which}{CommandLine} of {count}"
    # And it renders: EventId is neither grouped nor included.
    tmpl.fill("{which} of {count} ")
    fl_sheet.evaluate(DROP_AT, [FIELD.format("EventId"), TMPL, "end"])
    fl_sheet.wait_for_function(
        f"() => ({DESCS})().every((d) => /^(First|Last|Only) of \\d+ \\d+$/.test(d))", timeout=10_000)


def test_a_field_dropped_on_a_function_fills_it_in(fl_sheet):
    _grouped(fl_sheet)
    tmpl = fl_sheet.locator(TMPL).first
    tmpl.fill("{which}: ")
    fl_sheet.evaluate(DRAG, [FIELD.format("EventId"), "[data-fn='unique']"])
    assert _value(fl_sheet) == "{which}: {unique:EventId}"
    # Each host saw all four event ids (ui.csv cycles them).
    fl_sheet.wait_for_function(
        f"() => {{ const d = ({DESCS})(); return d.length > 0 && d.every((t) => /^(First|Last|Only): 4$/.test(t)); }}",
        timeout=10_000)
    tmpl.fill("")
    fl_sheet.evaluate(DRAG, [FIELD.format("EventId"), "[data-fn='max']"])
    assert _value(fl_sheet) == "{max:EventId}"
    fl_sheet.wait_for_function(f"() => ({DESCS})().every((d) => d === '4688')", timeout=10_000)


def test_a_text_field_on_a_total_says_why_and_inserts_nothing(fl_sheet):
    tmpl = fl_sheet.locator(TMPL).first
    tmpl.fill("{which}")
    fl_sheet.evaluate(DRAG, [FIELD.format("Host"), "[data-fn='sum']"])
    fl_sheet.wait_for_selector("#toast:not([hidden]):has-text('takes a number column')", timeout=5_000)
    assert _value(fl_sheet) == "{which}"


def test_a_function_click_opens_the_completion_for_it(fl_sheet):
    tmpl = fl_sheet.locator(TMPL).first
    tmpl.fill("{which} ")
    fl_sheet.locator("[data-fn='values']").click()
    assert _value(fl_sheet) == "{which} {values:"
    fl_sheet.wait_for_selector(".fl-ac .menu-item", timeout=5_000)
    items = fl_sheet.evaluate("() => [...document.querySelectorAll('.fl-ac .menu-item')]"
                              ".map((n) => n.firstChild.textContent)")
    assert items and all(n.startswith("values:") for n in items), items
    fl_sheet.keyboard.type("Ho")
    fl_sheet.keyboard.press("Enter")
    fl_sheet.wait_for_function("() => !document.querySelector('.fl-ac')")
    assert _value(fl_sheet) == "{which} {values:Host}"
    assert fl_sheet.evaluate(f"() => document.activeElement === document.querySelector(\"{TMPL}\")")


def test_count_unique_adds_a_column_of_each_groups_count(fl_sheet):
    _grouped(fl_sheet)
    fl_sheet.evaluate(DRAG, [FIELD.format("EventId"), "[data-zone='unique']"])
    fl_sheet.wait_for_function(
        "() => [...document.querySelectorAll('table thead th')].map((th) => th.textContent.trim())"
        ".join('|').includes('Unique count of EventId')", timeout=10_000)
    i = fl_sheet.evaluate("() => [...document.querySelectorAll('table thead th')]"
                          ".findIndex((th) => th.textContent.trim().startsWith('Unique count of EventId'))")
    cells = fl_sheet.evaluate(f"() => [...document.querySelectorAll('table tbody tr')].map((tr) => tr.children[{i}].textContent)")
    assert cells and set(cells) == {"4"}
    # A text column counts too — Count unique is not Total up.
    fl_sheet.evaluate(DRAG, [FIELD.format("CommandLine"), "[data-zone='unique']"])
    fl_sheet.wait_for_selector("[data-zone='unique'] [data-field='CommandLine']", timeout=5_000)
