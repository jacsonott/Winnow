"""The First/Last page, round three, as an analyst meets it: the table
picker said the imported file's name where the rest of the app said the
nickname, a new sheet opened with the last description's default already
typed in, the description sat after the whole-row JSON cell, and there was
no way to list a group's distinct values."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

TMPL = "input[style*='var(--mono)']"


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


def _headers_are(pg, want):
    pg.wait_for_function(
        "(want) => [...document.querySelectorAll('thead th')].map((h) => h.textContent).join('|') === want",
        arg=want, timeout=10_000)


def test_the_table_picker_shows_a_nickname(server_call, server_post, first_last_page):
    sid = next(s["id"] for s in server_call("/api/sources") if s["name"] == "ui.csv")
    server_post(f"/api/source/{sid}/nickname", {"nickname": "Logons"})
    try:
        with first_last_page() as pg:
            opt = pg.locator(f"select option[value='{sid}']").first
            pg.wait_for_function(
                f"() => (document.querySelector(\"select option[value='{sid}']\") || {{}}).textContent === 'Logons (200)'",
                timeout=10_000)
            # The file it came from is still a hover away.
            assert "ui.csv" in opt.get_attribute("title")
    finally:
        # The case is shared with every other UI module; leave it as found.
        server_post(f"/api/source/{sid}/nickname", {"nickname": ""})


def test_a_new_sheet_starts_with_an_empty_description(fl_sheet, fl_new_sheet):
    tmpl = fl_sheet.locator(TMPL).first
    tmpl.fill("{which} at {Host}")
    fl_new_sheet(fl_sheet)
    assert tmpl.input_value() == ""
    # Empty is not blank output: the box says what it will read as.
    assert tmpl.get_attribute("placeholder") == "{which} of {count}"
    fl_sheet.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    fl_sheet.wait_for_function(
        "() => { const td = document.querySelector('table tbody tr td:last-child');"
        " return !!td && /^(First|Last|Only) of \\d+$/.test(td.textContent.trim()); }", timeout=10_000)
    # ...and the sheet it came from kept its own.
    fl_sheet.locator(".sql-tab").nth(fl_sheet.locator(".sql-tab").count() - 3).click()
    fl_sheet.wait_for_function(f"() => document.querySelector(\"{TMPL}\").value === '{{which}} at {{Host}}'",
                         timeout=10_000)


def test_the_description_reads_before_the_json_cell(fl_sheet):
    fl_sheet.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    _headers_are(fl_sheet, "Timestamp|Host|Description")
    fl_sheet.get_by_text("Add the whole row as a JSON cell").click()
    _headers_are(fl_sheet, "Timestamp|Host|Description|Row (JSON)")


def test_list_values_adds_a_column_of_each_groups_values(fl_sheet):
    fl_sheet.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    fl_sheet.evaluate(DRAG, ["[data-field='EventId']", "[data-zone='values']"])
    _headers_are(fl_sheet, "Timestamp|Host|Values of EventId|Description")
    # Every event id each host saw, not the two bookends' — by value,
    # since EventId is a number column.
    cells = fl_sheet.evaluate("() => [...document.querySelectorAll('table tbody tr')]"
                        ".map((tr) => tr.children[2].textContent)")
    assert cells and set(cells) == {"1, 4624, 4625, 4688"}
    # The placeholder renders the same list: the field dropped on the
    # {values:…} function fills it in.
    fl_sheet.evaluate(DRAG, ["[data-field='EventId']", "[data-fn='values']"])
    assert fl_sheet.evaluate(f"() => document.querySelector(\"{TMPL}\").value") == "{values:EventId}"
    fl_sheet.wait_for_function(
        "() => [...document.querySelectorAll('table tbody tr td:last-child')]"
        ".every((td) => td.textContent.endsWith('1, 4624, 4625, 4688'))", timeout=10_000)


def test_which_number_puts_the_position_in_the_description(fl_sheet):
    fl_sheet.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    fl_sheet.wait_for_selector("table tbody tr", timeout=10_000)
    fl_sheet.locator(TMPL).first.fill("{which:number} of {count}")
    fl_sheet.wait_for_function(
        "() => { const d = [...document.querySelectorAll('table tbody tr td:last-child')]"
        ".map((td) => td.textContent); return d.includes('1 of 40') && d.includes('40 of 40'); }",
        timeout=10_000)
