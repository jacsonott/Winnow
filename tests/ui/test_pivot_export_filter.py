"""The pivot's way out, and its field filter. Copy, CSV and a filter on a
field (not a tag) had never been pressed in a test: the tag chips, the
drill-down and the state round trip were all that ran.

Copy puts the cross-tab on the clipboard as tab-separated text, the shape a
spreadsheet pastes into cells. CSV downloads the same cells, with the guard
Winnow's own CSV export uses: a cell that starts with = + - or @ is a
formula to Excel, and these files are opened in Excel by definition, so it
goes out with a ' in front. A field filter is Excel's filter dropdown: pick
values off the column's real list (searchable, All/None), or type an
operand for an operator that takes one.

A table of its own, so the values (one of them a formula, one with a comma)
are the ones these tests describe, deleted again at the end of the module.
"""

from __future__ import annotations

import pytest

from test_pivot_tags import DRAG_FIELD, _is_aggregate

pytestmark = pytest.mark.ui

KEY = "tab:pivot-table.pivot"
ROWS = [("WKS1", "alice"), ("WKS1", "bob"), ("WKS1", "alice"), ("WKS2", "carol"),
        ("WKS2", "alice"), ("=SUM(1,2)", "mallory"), ("SRV, east", "bob")]


@pytest.fixture(scope="module")
def table(tmp_path_factory, server_post, server_call):
    path = tmp_path_factory.mktemp("pivot_export") / "pv_out.csv"
    path.write_text("Host,User\n" + "".join(f'"{h}","{u}"\n' for h, u in ROWS), encoding="utf-8")
    sid = server_post("/api/ingest/path", {"path": str(path), "name": path.name, "build_fts": False})["id"]
    try:
        yield sid
    finally:
        server_call(f"/api/source/{sid}", "DELETE")


@pytest.fixture(scope="module")
def pv(own_page, server_post, table):
    server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "on_all"})
    server_post("/api/plugin_state", {"key": KEY, "payload": None})
    try:
        with own_page(permissions=["clipboard-read", "clipboard-write"]) as pg:
            pg.evaluate("() => __winnow.loadPlugins()")
            pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('pivot'))",
                                 timeout=10_000)
            pg.locator(".tab-plugin", has_text="Pivot").click()
            pg.wait_for_selector(".pluginview [data-zone='rows']", timeout=10_000)
            sel = ".pluginview select"
            pg.wait_for_function("(id) => [...document.querySelector('.pluginview select').options]"
                                 ".some((o) => o.value === String(id))", arg=table, timeout=10_000)
            pg.select_option(sel, str(table))
            pg.wait_for_selector(".pluginview [data-field='User']", timeout=10_000)
            yield pg
    finally:
        server_post("/api/plugin_state", {"key": KEY, "payload": None})
        server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "off_all"})


def _button(pg, label):
    return pg.locator(".pluginview .btn", has_text=label).first


def _toast(pg, text):
    pg.wait_for_function("(t) => (document.getElementById('toast').textContent || '').includes(t)", arg=text)


def _clear(pg):
    _button(pg, "Clear").click()
    pg.wait_for_selector(".pluginview table", state="detached", timeout=10_000)


def _count_of_host_by_host(pg):
    pg.evaluate(DRAG_FIELD, ["[data-field='Host']", "[data-zone='rows']"])
    with pg.expect_response(_is_aggregate, timeout=15_000):
        pg.evaluate(DRAG_FIELD, ["[data-field='Host']", "[data-zone='values']"])
    pg.wait_for_selector(".pluginview table tbody tr", timeout=15_000)


GRAND_TOTAL = """() => {
  const rows = document.querySelectorAll('.pluginview table tbody tr');
  const cell = rows.length ? rows[rows.length - 1].lastElementChild : null;
  return cell ? cell.textContent.replace(/,/g, '') : null;
}"""


def _total_is(pg, n):
    pg.wait_for_function(f"() => ({GRAND_TOTAL})() === '{n}'", timeout=15_000)


def _cells(pg):
    return pg.evaluate("""() => [...document.querySelectorAll('.pluginview table tr')]
      .map((tr) => [...tr.children].map((c) => c.textContent))""")


def test_with_nothing_pivoted_there_is_nothing_to_copy_or_download(pv):
    _clear(pv)
    _button(pv, "Copy").click()
    _toast(pv, "Nothing to copy yet")
    _button(pv, "CSV").click()
    _toast(pv, "Nothing to export yet")


def test_copy_puts_the_cross_tab_on_the_clipboard_as_tsv(pv, copied):
    _clear(pv)
    _count_of_host_by_host(pv)
    _total_is(pv, 7)
    cells = _cells(pv)
    with copied(pv) as clip:
        _button(pv, "Copy").click()
    assert clip.text == "\n".join("\t".join(r) for r in cells)
    lines = clip.text.split("\n")
    by_host = dict(line.split("\t") for line in lines[1:-1])
    assert by_host == {"=SUM(1,2)": "1", "SRV, east": "1", "WKS1": "3", "WKS2": "2"}
    assert lines[-1].split("\t")[-1] == "7"
    _toast(pv, "Pivot copied")


def test_csv_downloads_the_same_cells_with_formulas_defused(pv):
    _clear(pv)
    _count_of_host_by_host(pv)
    _total_is(pv, 7)
    cells = _cells(pv)
    with pv.expect_download() as dl:
        _button(pv, "CSV").click()
    d = dl.value
    assert d.suggested_filename == "pv_out-pivot.csv"
    text = open(d.path(), encoding="utf-8", newline="").read()
    lines = text.split("\r\n")
    assert len(lines) == len(cells), text
    # The formula leaves as text, quoted; the comma is quoted; the rest is bare.
    assert "\"'=SUM(1,2)\",1" in lines
    assert '"SRV, east",1' in lines
    assert "WKS1,3" in lines and "WKS2,2" in lines
    assert lines[-1].split(",")[-1] == "7"
    _toast(pv, "CSV downloaded")


def _filter_on_user(pg):
    pg.evaluate(DRAG_FIELD, ["[data-field='User']", "[data-zone='filters']"])
    chip = pg.locator(".pluginview [data-zone='filters'] [data-field='User']")
    chip.wait_for()
    return chip


def _open_filter(pg, chip):
    chip.click()
    pg.wait_for_selector("#modal:not([hidden])")
    # textContent: the title is upper-cased by CSS, which inner_text reports.
    assert pg.evaluate("() => document.getElementById('modalTitle').textContent") == "Filter — User"


def _value_rows(pg):
    return pg.locator("#modalBody label").all_inner_texts()


def _apply(pg):
    with pg.expect_response(_is_aggregate, timeout=15_000):
        pg.locator("#modalBody .btn", has_text="Apply").click()


def test_a_field_filter_keeps_the_values_picked_off_its_list(pv):
    _clear(pv)
    _count_of_host_by_host(pv)
    chip = _filter_on_user(pv)
    assert "all" in chip.inner_text()
    _open_filter(pv, chip)
    pv.wait_for_selector("#modalBody input[type=search]")
    # The column's real values, each with its count.
    rows = [r.split("\n") if "\n" in r else r.split() for r in _value_rows(pv)]
    assert {r[0]: r[-1] for r in rows} == {"alice": "3", "bob": "2", "carol": "1", "mallory": "1"}

    # Search narrows the list; ticking what is left keeps those rows.
    pv.locator("#modalBody input[type=search]").fill("ali")
    pv.wait_for_function("() => document.querySelectorAll('#modalBody label').length === 1")
    pv.locator("#modalBody label", has_text="alice").locator("input").check()
    _apply(pv)
    _total_is(pv, 3)
    assert "is any of 1" in chip.inner_text()

    # None, then All: every value, so every row.
    _open_filter(pv, chip)
    pv.wait_for_selector("#modalBody input[type=search]")
    pv.locator("#modalBody .btn", has_text="None").click()
    assert not any(pv.locator("#modalBody label input").evaluate_all("(cs) => cs.map((c) => c.checked)"))
    pv.locator("#modalBody .btn", has_text="All").click()
    assert all(pv.locator("#modalBody label input").evaluate_all("(cs) => cs.map((c) => c.checked)"))
    _apply(pv)
    _total_is(pv, 7)
    assert "is any of 4" in chip.inner_text()


def test_a_field_filter_with_a_typed_operand(pv):
    _clear(pv)
    _count_of_host_by_host(pv)
    chip = _filter_on_user(pv)
    _open_filter(pv, chip)
    pv.locator("#modalBody select").select_option("contains")
    box = pv.locator("#modalBody input:not([type=search])")
    box.wait_for()
    box.fill("o")   # bob, carol, mallory: not alice
    _apply(pv)
    _total_is(pv, 4)
    assert "contains o" in chip.inner_text()

    # An operator that takes no value says so, and needs none.
    _open_filter(pv, chip)
    pv.locator("#modalBody select").select_option("not_empty")
    pv.wait_for_selector("#modalBody .note-status:has-text('No value needed.')")
    _apply(pv)
    _total_is(pv, 7)
    assert "is not empty" in chip.inner_text()
