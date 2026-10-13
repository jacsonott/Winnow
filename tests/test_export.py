"""store.py export: CSV formula-injection prefixing (without mutating the
stored value), tagged_only filtering, and the tagged-rows xlsx workbook."""

from __future__ import annotations

import csv
import io

from openpyxl import load_workbook


def _export_csv_text(store, view_id, tagged_only=False):
    return "".join(store.export_view_csv(view_id, tagged_only=tagged_only))


def test_csv_export_prefixes_formula_leads_without_mutating_stored_value(store, write_csv):
    path = write_csv([
        ["Note"],
        ["=cmd|' /C calc'!A1"],
        ["+1+1"],
        ["-1+1"],
        ["@SUM(1,1)"],
        ["plain text"],
    ])
    rec = store.ingest_csv(path, name="formulas.csv", build_fts=False)
    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": []})

    text = _export_csv_text(store, view["view_id"])
    rows = list(csv.reader(io.StringIO(text)))
    body = rows[1:]
    values = [r[-1] for r in body]  # last column is "Note"
    prefixed = {v for v in values if v.startswith("'")}
    assert "'=cmd|' /C calc'!A1" in prefixed
    assert "'+1+1" in prefixed
    assert "'-1+1" in prefixed
    assert "'@SUM(1,1)" in prefixed
    assert "plain text" in values  # untouched

    # The exported copy is prefixed, but the stored value in the case file
    # itself must be byte-for-byte what was ingested — export never mutates
    # source data.
    stored = [r["cells"][0] for r in store.fetch_rows(view["view_id"], 0, 10)["rows"]]
    assert "=cmd|' /C calc'!A1" in stored
    assert "+1+1" in stored


def test_csv_export_tagged_only(ingested):
    store, source_id = ingested
    tag = store.upsert_tag(None, "Keep", "#ff0000", None)
    store.set_tags(source_id, [1], tag["id"], True)
    view = store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": []})

    all_rows = list(csv.reader(io.StringIO(_export_csv_text(store, view["view_id"]))))
    tagged_rows = list(csv.reader(io.StringIO(_export_csv_text(store, view["view_id"], tagged_only=True))))
    assert len(all_rows) - 1 == 4  # header + 4 fixture rows
    assert len(tagged_rows) - 1 == 1
    assert tagged_rows[1][0] == "1"  # Line column is the rid
    assert tagged_rows[1][1] == "Keep"


def test_xlsx_export_one_sheet_per_tagged_source(store, write_csv):
    p1 = write_csv([["A"], ["1"], ["2"]], name="x1.csv")
    p2 = write_csv([["A"], ["3"]], name="x2.csv")  # never tagged -> no sheet
    rec1 = store.ingest_csv(p1, name="x1.csv", build_fts=False)
    store.ingest_csv(p2, name="x2.csv", build_fts=False)
    tag = store.upsert_tag(None, "T", "#000000", None)
    store.set_tags(rec1["id"], [1], tag["id"], True)

    buf = store.export_tagged_xlsx()
    wb = load_workbook(buf)
    assert wb.sheetnames == ["x1.csv"]  # only the tagged source gets a sheet
    ws = wb["x1.csv"]
    header = [c.value for c in ws[1]]
    assert header == ["Line", "Tags", "Note", "A"]
    data_rows = [[c.value for c in row] for row in ws.iter_rows(min_row=2)]
    assert data_rows == [[1, "T", None, "1"]]


def test_xlsx_export_empty_case_still_produces_a_workbook(store):
    buf = store.export_tagged_xlsx()
    wb = load_workbook(buf)
    assert wb.sheetnames == ["No tagged rows"]


def test_csv_export_includes_derived_columns(store, write_csv):
    """A regex/timestamp/JSON derived column is part of the table the
    analyst sees — the export has to carry it like any base column."""
    path = write_csv([["Uri"], ["https://a.example/x"], ["https://b.example/y"]])
    rec = store.ingest_csv(path, name="uri.csv", build_fts=False)
    res = store.add_derived_column(rec["id"], "Host", "Uri", "regex_extract",
                                   {"pattern": r"^\w+://([^/]+)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": []})
    text = _export_csv_text(store, view["view_id"])
    lines = text.strip().splitlines()
    assert lines[0].endswith(",Host")
    assert lines[1].endswith(",a.example") and lines[2].endswith(",b.example")


def test_xlsx_sheet_names_survive_control_characters_truncation_and_reserved_names(store, write_csv, tmp_path):
    """A sheet name is XML text in workbook.xml: a BEL in a source name
    (names keep what the file was called) produced a workbook that would
    not open again. The 31-character cut could also end a name in an
    apostrophe Excel refuses there, and "History" is reserved."""
    path = write_csv([["a"], ["1"]], name="x.csv")
    tag = store.upsert_tag(None, "T", "#000000", None)
    for name in ["evil\x07name.csv", "A" * 30 + "'s log.csv", "History"]:
        sid = store.ingest_csv(path, name=name, build_fts=False)["id"]
        store.set_tags(sid, [1], tag["id"], True)

    tagged = load_workbook(store.export_tagged_xlsx()).sheetnames
    out = tmp_path / "all.xlsx"
    store.export_all_xlsx(str(out))
    for names in (tagged, load_workbook(str(out)).sheetnames):
        assert len(names) == 3
        for n in names:
            assert len(n) <= 31 and not n.startswith("'") and not n.endswith("'")
            assert not any(ord(ch) < 32 for ch in n)
            assert n.lower() != "history"
        assert names[0] == "evil_name.csv"


def test_xlsx_cell_too_long_for_excel_is_marked_not_silently_cut(store, write_csv):
    """Excel holds 32,767 characters per cell and openpyxl cut longer
    values there without a word — an encoded command line that looks
    complete and isn't. The cut is marked, and says how long it was."""
    path = write_csv([["cmd"], ["A" * 40000], ["short"]], name="long.csv")
    sid = store.ingest_csv(path, name="long.csv", build_fts=False)["id"]
    tag = store.upsert_tag(None, "T", "#000000", None)
    store.set_tags(sid, [1, 2], tag["id"], True)
    ws = load_workbook(store.export_tagged_xlsx()).worksheets[0]
    cell = ws["D2"].value
    assert len(cell) <= 32767
    assert cell.startswith("A" * 30000) and "cut" in cell and "40,000" in cell
    assert ws["D3"].value == "short"


def test_csv_export_of_a_merge_says_which_table_each_row_is_from(store, write_csv):
    """Line is a rid within ONE member, so a merged export had two rows
    reading "Line 1" and nothing saying which file either came from. A
    merge's export names the member (as the sidebar does, nickname
    first); a single table's export is unchanged."""
    a = store.ingest_csv(write_csv([["user"], ["alice"], ["bob"]], name="hostA.csv"),
                         name="hostA_Security.csv", build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["user"], ["carol"], ["dave"]], name="hostB.csv"),
                         name="hostB_Security.csv", build_fts=False)["id"]
    store.set_source_nickname(b, "Host B")
    tag = store.upsert_tag(None, "Keep", "#ff0000", None)
    store.set_tags(a, [1], tag["id"], True)
    store.set_tags(b, [1], tag["id"], True)
    mid = store.create_merge("M", [a, b])["id"]
    view = store.build_view(mid, {"filters": [], "sort": [{"column": "user", "dir": "asc"}]})

    rows = list(csv.reader(io.StringIO(_export_csv_text(store, view["view_id"], tagged_only=True))))
    assert rows[0] == ["Line", "Table", "Tags", "Note", "user"]
    assert rows[1:] == [["1", "hostA_Security.csv", "Keep", "", "alice"],
                        ["1", "Host B", "Keep", "", "carol"]]

    single = store.build_view(a, {"filters": [], "sort": [{"column": "user", "dir": "asc"}]})
    assert next(csv.reader(io.StringIO(_export_csv_text(store, single["view_id"])))) == \
        ["Line", "Tags", "Note", "user"]
