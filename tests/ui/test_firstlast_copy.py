"""First/Last's Copy result: the WHOLE result (the backend's rows job, not
the preview's first groups) as tab-separated text. It had never been
pressed in a test.

What it owes the analyst: every group's first and last row, in a shape a
spreadsheet pastes into cells. And because the job can take a while on a
big table and nothing waits on it, an analyst who leaves the tab is not
surprised by a clipboard that changed under them: the answer lands as a
card with a Copy button, and only that click writes the clipboard (the
analyst may have copied something else in the meantime).

ui.csv is the shared session table: 200 rows, Host H0..H4 in turn, one
second apart from 08:00:00, so each host's first and last rows are known.
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

NOTICES = "#jobsPanel:not([hidden]) .job-row.job-notice"
COPY = ".pluginview .btn:text-is('Copy result')"


def _ts(i):
    return f"2026-03-14 08:{i // 60:02d}:{i % 60:02d}"


# Host Hk is rows k, k+5, ... k+195: first at k, last at 195+k.
BOOKENDS = {f"H{k}": {_ts(k), _ts(195 + k)} for k in range(5)}


@pytest.fixture
def sheet(fl_page, fl_new_sheet, server_call):
    """A new sheet on ui.csv, closed again afterwards (sheets are saved in
    the case, and the case is every module's)."""
    pg = fl_page
    fl_new_sheet(pg)
    sid = next(s["id"] for s in server_call("/api/sources") if s["name"] == "ui.csv")
    pg.select_option(".pluginview select[title='Which table to group']", str(sid))
    pg.wait_for_selector(".pluginview [data-field='Host']", timeout=10_000)
    try:
        yield pg
    finally:
        pg.unroute("**/job/start")
        pg.evaluate("() => { const m = document.getElementById('modal'); if (m) m.hidden = true; }")
        if not pg.locator(".pluginview").is_visible():
            pg.locator(".tab-plugin", has_text="First/Last").click()
            pg.wait_for_selector(".pluginview [data-zone='groupBy']", timeout=10_000)
        pg.locator(".sql-tab[aria-selected='true'] span[title='Close this sheet']").click()


def _toast(pg, text):
    pg.wait_for_function("(t) => (document.getElementById('toast').textContent || '').includes(t)", arg=text)


def _check_bookends(text):
    lines = text.split("\n")
    header = lines[0].split("\t")
    assert "Host" in header and "Timestamp" in header, header
    h, t = header.index("Host"), header.index("Timestamp")
    rows = [line.split("\t") for line in lines[1:]]
    assert all(len(r) == len(header) for r in rows), lines
    got = {}
    for r in rows:
        got.setdefault(r[h], set()).add(r[t])
    assert got == BOOKENDS


def test_copy_result_with_no_grouping_says_what_is_missing(sheet):
    sheet.locator(COPY).click()
    _toast(sheet, "Build a grouping first")


def test_copy_result_puts_every_groups_bookends_on_the_clipboard(sheet, copied):
    sheet.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    with copied(sheet) as clip:
        sheet.locator(COPY).click()
    _check_bookends(clip.text)
    assert len(clip.text.split("\n")) == 1 + 10
    _toast(sheet, "Copied 10 rows")


def test_away_from_the_tab_the_result_waits_on_a_card(sheet, copied, until):
    """Held at the server's door until the analyst has gone to the grid,
    so the answer lands while the tab is hidden, as it does on a big
    table."""
    sheet.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    held = []
    sheet.route("**/job/start", lambda r: held.append(r)
                if (r.request.post_data_json or {}).get("kind") == "rows" else r.continue_())

    with copied(sheet) as clip:
        sheet.locator(COPY).click()
        until(sheet, lambda: held, "the rows job to start")
        assert sheet.locator(".pluginview .btn:text-is('Copying…')").is_disabled()
        sheet.evaluate("() => __winnow.showGridTab()")
        sheet.wait_for_selector(".pluginview", state="hidden")
        # Gone from the tab, the job says so on a card.
        sheet.wait_for_selector(f"{NOTICES}:has(.job-name:text-is('First/Last result'))")
        held[0].continue_()
        card = sheet.locator(NOTICES, has=sheet.locator(".job-detail", has_text="10 rows ready to copy"))
        card.wait_for(timeout=15_000)
        # The clipboard is not this tab's to write until the click.
        assert sheet.evaluate("() => navigator.clipboard.readText()") == clip.MARK
        card.locator(".job-action", has_text="Copy").click()
    _check_bookends(clip.text)
    _toast(sheet, "Copied 10 rows")
