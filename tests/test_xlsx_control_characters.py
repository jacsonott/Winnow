"""A worksheet cannot hold every byte a case file can.

Winnow keeps control characters in cell VALUES on purpose: ingest strips
them from column names only, because a name has to be quotable while
cells are evidence and stay as they are. openpyxl refuses those same
characters, and refuses the whole workbook rather than the one cell --
so before this, a single BEL inside a single command line took down the
entire tagged export with an IllegalCharacterError naming the value but
not the row, and nothing an analyst could do about it.

They are escaped rather than dropped: these land in exactly the fields
worth reading closely, and a silently shortened string in an exported
artifact is worse than a visible \\x07.
"""

from __future__ import annotations

import io

import pytest
from openpyxl import load_workbook

from winnow.store import XLSX_MAX_ROWS, _xlsx_safe, rows_to_xlsx

# BEL, SOH, VT, FF: all legal in a SQLite text column, none legal in XML.
NASTY = "cmd.exe /c echo \x07 \x01 \x0b \x0c done"


# -------------------------------------------------------- the sanitizer

def test_control_characters_are_escaped_not_dropped():
    out = _xlsx_safe(NASTY)
    assert "\\x07" in out and "\\x01" in out and "\\x0b" in out and "\\x0c" in out
    assert "\x07" not in out, "the raw character must not survive into the cell"
    # Nothing else moved: the readable text is still readable.
    assert out.startswith("cmd.exe /c echo ") and out.endswith(" done")


def test_the_three_legal_whitespace_characters_are_kept():
    """Tab, newline and carriage return are valid XML and meaningful in a
    cell. Escaping them would mangle every multi-line value in the case."""
    assert _xlsx_safe("a\tb\nc\rd") == "a\tb\nc\rd"


def test_it_still_does_what_the_csv_sanitizer_does():
    """_xlsx_safe wraps _csv_safe rather than replacing it — formula
    injection is as much a risk in .xlsx as in .csv."""
    assert _xlsx_safe("=SUM(A1)") == "'=SUM(A1)"
    assert _xlsx_safe("@cmd") == "'@cmd"


def test_a_value_with_nothing_wrong_is_returned_unchanged():
    assert _xlsx_safe("ordinary text") == "ordinary text"
    assert _xlsx_safe(None) is None
    assert _xlsx_safe(42) == 42


# ------------------------------------------------- the result workbook

def test_a_result_round_trips_through_a_workbook():
    buf = rows_to_xlsx(["Computer", "EventId"], [["DC01", 4624], ["WKSTN-02", 4625]], "Logons")
    ws = load_workbook(io.BytesIO(buf.read())).active
    assert ws.title == "Logons"
    assert [c.value for c in ws[1]] == ["Computer", "EventId"]
    assert [c.value for c in ws[2]] == ["DC01", 4624]
    assert ws.max_row == 3


def test_a_result_carrying_control_characters_still_writes():
    """The bug, at the level the analyst meets it: one bad cell used to
    cost the whole file."""
    buf = rows_to_xlsx(["CommandLine"], [[NASTY], ["clean row"]])
    ws = load_workbook(io.BytesIO(buf.read())).active
    assert "\\x07" in ws.cell(row=2, column=1).value
    assert ws.cell(row=3, column=1).value == "clean row"


def test_a_sheet_name_excel_would_refuse_is_cleaned():
    """Query tabs are named by the analyst, and / : * ? [ ] are ordinary
    things to type. The name is the default sheet name."""
    buf = rows_to_xlsx(["a"], [["x"]], "logons: DC01/DC02 [draft]?")
    ws = load_workbook(io.BytesIO(buf.read())).active
    assert ws.title == "logons_ DC01_DC02 _draft__"[:31]
    assert len(ws.title) <= 31


def test_more_rows_than_a_worksheet_holds_is_refused_with_a_reason(monkeypatch):
    """Refused up front rather than written as a corrupt file. Shrink the
    cap rather than build a million rows."""
    monkeypatch.setattr("winnow.store.XLSX_MAX_ROWS", 5)
    with pytest.raises(ValueError, match="more than one worksheet holds"):
        rows_to_xlsx(["a"], [[i] for i in range(5)])
    assert XLSX_MAX_ROWS == 1_048_576, "the real cap is Excel's, not the test's"


# --------------------------------------------- the exports that crashed

def _tagged_case(store, tmp_path, value):
    p = tmp_path / "evt.csv"
    p.write_text(f'Line,CommandLine\n1,"{value}"\n2,"ordinary"\n', encoding="utf-8", newline="")
    rec = store.ingest_csv(str(p), name="evt.csv")
    tag = store.upsert_tag(None, "TA", "#f00", None)
    store.set_tags(rec["id"], [1, 2], tag["id"], True)
    return rec


def test_the_tagged_export_survives_a_control_character(store, tmp_path):
    _tagged_case(store, tmp_path, NASTY)
    ws = load_workbook(io.BytesIO(store.export_tagged_xlsx().read())).active
    cells = [c.value for row in ws.iter_rows() for c in row if isinstance(c.value, str)]
    assert any("\\x07" in c for c in cells), cells


def test_the_all_tables_export_survives_a_control_character(store, tmp_path):
    _tagged_case(store, tmp_path, NASTY)
    out = tmp_path / "all.xlsx"
    store.export_all_xlsx(str(out))
    ws = load_workbook(out).active
    cells = [c.value for row in ws.iter_rows() for c in row if isinstance(c.value, str)]
    assert any("\\x07" in c for c in cells), cells


def test_the_stored_value_is_never_touched(store, tmp_path):
    """Invariant #1 in spirit: the export is a copy. The case file still
    holds the bytes that were imported."""
    rec = _tagged_case(store, tmp_path, NASTY)
    store.export_tagged_xlsx()
    stored = store.run_sql(f'SELECT CommandLine FROM src_{rec["id"]} WHERE rid=1')["rows"][0][0]
    assert "\x07" in stored, "the case file must keep the raw byte"


# ------------------------------------------------------------- the route

def test_the_route_returns_a_workbook(client):
    r = client.post("/api/sql/export_xlsx", json={
        "columns": ["Computer", "EventId"],
        "rows": [["DC01", 4624]],
        "filename": "logons.xlsx", "sheet": "Logons"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert 'filename="logons.xlsx"' in r.headers["content-disposition"]
    ws = load_workbook(io.BytesIO(r.content)).active
    assert [c.value for c in ws[1]] == ["Computer", "EventId"]


def test_the_route_escapes_rather_than_500s(client):
    r = client.post("/api/sql/export_xlsx", json={"columns": ["c"], "rows": [[NASTY]]})
    assert r.status_code == 200
    ws = load_workbook(io.BytesIO(r.content)).active
    assert "\\x07" in ws.cell(row=2, column=1).value


def test_the_route_refuses_an_oversized_result(client, monkeypatch):
    monkeypatch.setattr("winnow.store.XLSX_MAX_ROWS", 3)
    r = client.post("/api/sql/export_xlsx", json={"columns": ["a"], "rows": [[1], [2], [3]]})
    assert r.status_code == 400
    assert "worksheet" in r.json()["detail"]
