"""The Add derived column dialog, opened from the Table dialog with no
column named, started on the table's first column — on an EvtxECmd table
that is RecordNumber — and the format guess called that counter "Mac
absolute (Cocoa) time" with 100% of its values parsing. It starts on the
first column that looks like a timestamp now, and a counter gets no
confident guess wherever it is picked."""
import time

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture
def evtx_table(page, server_post, tmp_path):
    f = tmp_path / "evtx.csv"
    lines = ["RecordNumber,EventRecordId,EventId,TimeCreated,Computer"]
    for i in range(1, 61):
        lines.append(f"{i},{100000 + i},{[4624, 4625, 4688][i % 3]},2026-03-03 00:00:{i % 60:02d},DC01")
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    server_post("/api/ingest/jobs/path", {"path": str(f), "name": "evtx.csv", "kind": "csv"})
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        names = page.evaluate("() => __winnow.loadSources().then(() => __winnow.S.sources.map((s) => s.name))")
        if "evtx.csv" in names:
            break
        time.sleep(0.25)
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'evtx.csv').id")
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_selector(".row")
    yield sid
    page.evaluate("() => __winnow.closeModal()")
    page.evaluate("(id) => __winnow.api('/api/source/' + id, { method: 'DELETE' }).then(() => __winnow.loadSources())", sid)


def test_opened_from_the_table_it_starts_on_the_timestamp(page, evtx_table, format_detected):
    page.evaluate("() => __winnow.openDerivedColumnModal()")
    page.wait_for_selector("#modalBody select[data-role=column]")
    assert page.locator("#modalBody select[data-role=column]").input_value() == "TimeCreated"
    assert "Mac absolute" not in format_detected()


def test_a_counter_gets_no_confident_guess_but_can_still_be_converted(page, evtx_table, format_detected):
    page.evaluate("() => __winnow.openDerivedColumnModal('RecordNumber')")
    page.wait_for_selector("#modalBody select[data-role=column]")
    note = format_detected()
    assert note == "No format detected — pick one below to see what it produces.", note
    # The analyst's own choice still stands: picked by hand, Mac time reads
    # every value and the preview says so.
    page.locator("#modalBody select[data-role=op]").select_option(label="Mac absolute (Cocoa) time")
    page.wait_for_function(
        "() => (document.querySelector('#modalBody .derived-verdict') || {}).textContent?.startsWith('All ')")
    assert page.locator("#modalBody .derived-out").first.inner_text() == "2001-01-01 00:00:01"
    assert page.locator("#modalBody .btn.primary", has_text="Add column").is_enabled()


def test_the_name_says_time_when_no_column_is_typed_datetime(page):
    """A table whose times are numbers (a Chrome History export) has no
    datetime column to start on; the name is the next best evidence."""
    got = page.evaluate("""() => __winnow.defaultParseColumn([
        { name: 'id', type: 'number' }, { name: 'TimeZone', type: 'text' },
        { name: 'ModifiedBy', type: 'text' }, { name: 'visit_time', type: 'number' }])""")
    assert got == "visit_time"
    assert page.evaluate("() => __winnow.defaultParseColumn([{ name: 'id', type: 'number' }, { name: 'url', type: 'text' }])") == "id"
