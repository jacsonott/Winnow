"""Custom timestamp patterns in the browser: written in the Add derived
column dialog (Format → Custom pattern), kept with "Save as a format…",
offered again by detection, and managed in Settings → Timestamps.

The table's stamps ("14.03.2026 13:22:01,482", a European export with a
comma before the milliseconds) are read by no built-in format, which is
the case the feature exists for: before it, the dialog could only say "No
format detected" and the column stayed text."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

PATTERN = "dd.mm.yyyy hh:mm:ss,fff"
ROWS = ["14.03.2026 13:22:01,482", "14.03.2026 13:24:11,001", "15.03.2026 08:00:00,000"]


def _drop_formats(api):
    for f in api("/api/timestamp_formats"):
        api(f"/api/timestamp_formats/{f['id']}", "DELETE")


@pytest.fixture
def eu_table(page, imported, api, tmp_path):
    f = tmp_path / "fw_eu.csv"
    f.write_text("Logged,Action\n" + "".join(f'"{v}",allow\n' for v in ROWS * 20), encoding="utf-8")
    sid = imported({"path": str(f), "name": "fw_eu.csv", "kind": "csv"})[0]
    page.evaluate("() => __winnow.loadSources()")
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_selector(".row")
    try:
        yield sid
    finally:
        # Saved formats live in the server's workspace, not the case, so
        # the case snapshot would not see one left behind — but every
        # later module's detection would.
        page.evaluate("() => __winnow.closeModal()")
        _drop_formats(api)
        page.evaluate("(id) => __winnow.api('/api/source/' + id, { method: 'DELETE' }).then(() => __winnow.loadSources())", sid)


def _open(page, format_detected):
    page.evaluate("() => __winnow.openDerivedColumnModal('Logged')")
    page.wait_for_selector("#modalBody select[data-role=format]")
    return format_detected()


def test_a_pattern_reads_the_column_and_can_be_kept(page, eu_table, api, format_detected, derive_pick,
                                                    derive_preview):
    assert _open(page, format_detected) == "No format detected — pick one to see what it produces."
    derive_pick("Parse a timestamp", "Custom pattern")
    page.fill("#modalBody input[data-param=pattern]", PATTERN)
    derive_preview()
    assert page.locator("#modalBody .derived-out").first.inner_text() == "2026-03-14 13:22:01.482000"
    assert page.locator("#modalBody .derived-verdict").inner_text().startswith("All ")
    # What the pattern means, written for a day above 12.
    page.wait_for_function("() => document.querySelector('#modalBody .derived-pattern-example')"
                           ".textContent === 'Reads values like 14.03.2026 13:22:01,482'")

    page.locator("#modalBody .derived-save-format").click()
    page.fill(".confirm-input", "Firewall (EU)")
    page.locator(".confirm-actions .btn.primary").click()
    page.wait_for_function("() => document.querySelector('#modalBody select[data-role=format]').value.startsWith('saved:')")
    fmt = page.locator("#modalBody select[data-role=format]")
    assert fmt.evaluate("(s) => s.selectedOptions[0].textContent") == f"Firewall (EU) — {PATTERN}"
    assert fmt.evaluate("(s) => s.selectedOptions[0].parentElement.label") == "Your formats"
    # Nothing more to keep: the pattern is the saved format's.
    assert page.locator("#modalBody .derived-save-format").is_hidden()
    assert [(f["name"], f["pattern"]) for f in api("/api/timestamp_formats")] == [("Firewall (EU)", PATTERN)]

    page.locator("#modalBody .btn.primary", has_text="Add column").click()
    page.wait_for_function("() => (__winnow.S.columns.find((c) => c.name === 'Logged (parsed)') || {})"
                           ".derived_status === 'ready'", timeout=20_000)
    col = page.evaluate("() => __winnow.S.columns.find((c) => c.name === 'Logged (parsed)')")
    assert (col["type"], col["derived_op"], col["derived_params"]["pattern"]) == ("datetime", "custom_pattern", PATTERN)
    # The header's ƒ says which pattern made it.
    page.wait_for_function(
        "(p) => { const m = document.querySelector('.hcell[data-col=\"Logged (parsed)\"] .hcell-derived');"
        " return !!m && m.title.endsWith('Custom pattern ' + p); }", arg=PATTERN)


def test_a_saved_format_is_suggested_for_a_column_it_reads(page, eu_table, api, format_detected, derive_preview):
    rec = api("/api/timestamp_formats", "POST", {"name": "Firewall (EU)", "pattern": PATTERN})
    note = _open(page, format_detected)
    assert note == "Suggested: Firewall (EU) — 100% of sampled values parse.", note
    page.wait_for_function("(v) => document.querySelector('#modalBody select[data-role=format]').value === v",
                           arg=f"saved:{rec['id']}")
    assert page.locator("#modalBody input[data-param=pattern]").input_value() == PATTERN
    derive_preview()
    assert page.locator("#modalBody .derived-out").first.inner_text() == "2026-03-14 13:22:01.482000"


def test_a_bad_pattern_says_why_in_the_dialog(page, eu_table, format_detected, derive_pick, derive_preview):
    _open(page, format_detected)
    derive_pick("Parse a timestamp", "Custom pattern")
    page.fill("#modalBody input[data-param=pattern]", "dd.MM.yyyy MM")
    page.wait_for_function("() => document.querySelector('#modalBody .derived-pattern-example')"
                           ".textContent.startsWith('This pattern has two months.')")
    assert "bad" in page.locator("#modalBody .derived-pattern-example").get_attribute("class")
    derive_preview()
    assert "two months" in page.locator("#modalBody .derived-preview").inner_text()


def test_settings_adds_tries_edits_and_deletes_a_format(page, eu_table, api):
    page.evaluate("() => __winnow.openSettings('timestamps')")
    box = page.locator("[data-role=parse-formats]")
    page.wait_for_selector("[data-role=parse-formats] .tsf-empty")
    page.get_by_role("button", name="Add a format…").click()
    page.fill("[data-role=format-name]", "Oracle audit")
    page.fill("[data-role=format-pattern]", "dd-MMM-yy hh.mm.ss.ffffff tt")
    page.fill("[data-role=format-sample]", "14-MAR-26 01.22.01.482391 PM")
    page.wait_for_function("() => document.querySelector('[data-role=format-result]').textContent"
                           " === '→ 2026-03-14 13:22:01.482391'")
    page.get_by_role("button", name="Save format").click()
    row = box.locator(".tsf-row")
    page.wait_for_selector("[data-role=parse-formats] .tsf-row")
    assert row.locator(".tsf-name").inner_text() == "Oracle audit"
    assert row.locator(".tsf-example").inner_text() == "Reads values like 14-Mar-26 01.22.01.482391 PM"

    row.get_by_role("button", name="Edit").click()
    page.fill("[data-role=format-pattern]", "dd-MMM-yyyy")
    page.get_by_role("button", name="Save changes").click()
    page.wait_for_function("() => (document.querySelector('[data-role=parse-formats] .tsf-pattern') || {})"
                           ".textContent === 'dd-MMM-yyyy'")
    assert [f["pattern"] for f in api("/api/timestamp_formats")] == ["dd-MMM-yyyy"]

    row.get_by_role("button", name="Delete").click()
    page.locator(".confirm-actions .btn.danger").click()
    page.wait_for_selector("[data-role=parse-formats] .tsf-empty")
    assert api("/api/timestamp_formats") == []
