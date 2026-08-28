"""Excel workbook ingest, end to end: the xlsxread text conversions, the
sheet preview/ingest pair on Store, the multi-sheet background job, and
the HTTP routing. Fixtures are written with openpyxl — already a runtime
dependency for the XLSX export, and the same library the reader wraps."""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

import xlsxread
from store import BATCH


@pytest.fixture
def write_xlsx(tmp_path):
    """write_xlsx({sheet: rows}) -> path. Cell values keep their Python
    types (str/int/float/bool/datetime/None) — openpyxl styles datetimes
    as dates on its own, which is exactly the signal the reader keys on."""

    def _write(sheets: dict[str, list[list]], name: str = "book.xlsx") -> str:
        wb = Workbook()
        wb.remove(wb.active)
        for title, rows in sheets.items():
            ws = wb.create_sheet(title)
            for r in rows:
                ws.append(r)
        path = tmp_path / name
        wb.save(path)
        return str(path)

    return _write


def test_cell_text_conversions():
    assert xlsxread.cell_text(None) == ""
    assert xlsxread.cell_text("plain") == "plain"
    assert xlsxread.cell_text(True) == "TRUE"
    assert xlsxread.cell_text(False) == "FALSE"
    assert xlsxread.cell_text(42) == "42"
    assert xlsxread.cell_text(42.0) == "42"
    assert xlsxread.cell_text(3.25) == "3.25"
    assert xlsxread.cell_text(datetime.datetime(2024, 5, 1, 13, 7, 9)) == "2024-05-01 13:07:09"
    # Day-serials are floats: 999999µs of storage imprecision must round,
    # not print — and exact midnight renders as a bare date.
    assert xlsxread.cell_text(datetime.datetime(2024, 5, 1, 13, 6, 59, 999999)) == "2024-05-01 13:07:00"
    assert xlsxread.cell_text(datetime.datetime(2024, 5, 1)) == "2024-05-01"
    assert xlsxread.cell_text(datetime.date(2024, 5, 2)) == "2024-05-02"
    assert xlsxread.cell_text(datetime.time(9, 30, 5)) == "09:30:05"
    assert xlsxread.cell_text(datetime.timedelta(hours=26, minutes=3, seconds=4)) == "26:03:04"


def test_reader_rows_dates_and_gaps(write_xlsx):
    path = write_xlsx({"Events": [
        ["When", "Host", "Count"],
        [datetime.datetime(2024, 5, 1, 13, 7), "srv01", 42],
        [datetime.date(2024, 5, 2), None, 3.5],  # gap stays a gap, in place
    ]})
    hint_rows, hint_cols, rows = xlsxread.sheet_reader(path, "Events")
    assert (hint_rows, hint_cols) == (3, 3)
    assert list(rows) == [
        ["When", "Host", "Count"],
        ["2024-05-01 13:07:00", "srv01", "42"],
        ["2024-05-02", "", "3.5"],
    ]
    with pytest.raises(ValueError, match="No such sheet"):
        xlsxread.sheet_reader(path, "Nope")


def test_workbook_sheets_listing(write_xlsx):
    path = write_xlsx({
        "Events": [["When", "Host"], ["a", "b"], ["c", "d"]],
        "Empty": [],
        "HeaderOnly": [["X"]],
    })
    sheets = xlsxread.workbook_sheets(path)
    assert [s["name"] for s in sheets] == ["Events", "Empty", "HeaderOnly"]
    assert sheets[0] == {"name": "Events", "row_count": 2, "columns": ["When", "Host"]}
    assert sheets[1]["row_count"] == 0 and sheets[1]["columns"] == []
    assert sheets[2]["row_count"] == 0 and sheets[2]["columns"] == ["X"]


def test_preview_matches_sqlite_picker_shape(store, write_xlsx):
    path = write_xlsx({"Log": [["Time", "Msg"], ["t1", "m1"]]})
    res = store.preview_xlsx_sheets(path)
    assert res == {"tables": [{
        "name": "Log", "row_count": 1,
        "columns": [{"name": "Time", "type": "TEXT"}, {"name": "Msg", "type": "TEXT"}],
        "likely_timestamp_columns": [],
    }]}


def test_ingest_sheet_values_and_naming(store, write_xlsx):
    path = write_xlsx({"Events": [
        [None, None],                       # leading empty row — skipped
        ["When", "Host", "Host"],           # dupe headers go through sanitize_columns
        [datetime.datetime(2024, 5, 1, 8, 0, 30), "srv01", "x"],
        ["not-a-date", None, "y"],
    ]}, name="triage.xlsx")
    rec = store.ingest_xlsx_sheet(path, "Events", build_fts=False)
    assert rec["name"] == "triage.Events"
    assert rec["row_count"] == 2
    assert rec["ragged_rows"] == 0
    assert [c["name"] for c in rec["columns"]] == ["When", "Host", "Host_1"]
    got = store.db.execute(f"SELECT * FROM src_{rec['id']} ORDER BY rid").fetchall()
    assert [tuple(r)[1:] for r in got] == [
        ("2024-05-01 08:00:30", "srv01", "x"),
        ("not-a-date", "", "y"),
    ]


def test_ingest_empty_sheet_refused(store, write_xlsx):
    path = write_xlsx({"Blank": []})
    with pytest.raises(ValueError, match="has no rows"):
        store.ingest_xlsx_sheet(path, "Blank")
    assert store.list_sources() == []


def test_multi_sheet_job(store, write_xlsx):
    path = write_xlsx({
        "A": [["X"], ["1"], ["2"]],
        "B": [["Y"], ["3"]],
    })
    job = store.start_ingest_job("xlsx", path, options={
        "build_fts": False,
        "tables": [{"table": "A"}, {"table": "B", "name": "renamed"}],
    })
    assert job["tables_total"] == 2
    done = store.wait_for_ingest_job(job["job_id"], timeout=30)
    assert done["status"] == "done"
    assert done["tables_done"] == 2
    assert done["rows_done"] == 3
    names = [s["name"] for s in store.list_sources()]
    assert "book.A" in names and "renamed" in names


def test_cancel_drops_partial_sheet(store, write_xlsx):
    # More rows than one BATCH so the per-batch cancel hook actually fires.
    rows = [["N"]] + [[i] for i in range(BATCH + 50)]
    path = write_xlsx({"Big": rows})
    from store import IngestCancelled
    with pytest.raises(IngestCancelled):
        store.ingest_xlsx_sheet(path, "Big", build_fts=False, cancel=lambda: True)
    assert store.list_sources() == []


def test_http_routing(store, write_xlsx, monkeypatch):
    import server
    monkeypatch.setattr(server, "STORE", store)
    client = TestClient(server.app)
    headers = {"X-Timeline-Lite-Client": "1"}
    path = write_xlsx({"Log": [["A"], ["v"]]})

    # Extension routing: .xlsx auto-detects, preview returns the sheet list.
    res = client.post("/api/ingest/preview/path", json={"path": path}, headers=headers)
    assert res.status_code == 200
    assert [t["name"] for t in res.json()["tables"]] == ["Log"]

    # The job route refuses a sheetless xlsx import, same gate as sqlite.
    res = client.post("/api/ingest/jobs/path", json={"path": path}, headers=headers)
    assert res.status_code == 400
    assert "sheets" in res.json()["detail"]

    # Upload preview spools the whole workbook (a zip can't be prefix-sniffed).
    with open(path, "rb") as f:
        res = client.post("/api/ingest/xlsx/preview", headers=headers,
                          files={"file": ("book.xlsx", f)})
    assert res.status_code == 200
    assert res.json()["tables"][0]["row_count"] == 1

    res = client.post("/api/ingest/jobs/path",
                      json={"path": path, "tables": [{"table": "Log"}], "build_fts": False},
                      headers=headers)
    assert res.status_code == 200
    done = store.wait_for_ingest_job(res.json()["job_id"], timeout=30)
    assert done["status"] == "done" and done["rows_done"] == 1

    # And the upload transport: spool + background job, same tables option.
    with open(path, "rb") as f:
        res = client.post("/api/ingest/jobs/upload", headers=headers,
                          files={"file": ("book.xlsx", f)},
                          data={"kind": "xlsx", "build_fts": "false",
                                "tables": '[{"table": "Log", "name": "uploaded"}]'})
    assert res.status_code == 200
    done = store.wait_for_ingest_job(res.json()["job_id"], timeout=30)
    assert done["status"] == "done"
    assert "uploaded" in [s["name"] for s in store.list_sources()]
