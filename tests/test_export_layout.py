"""Exports emit the table as the analyst arranged it: saved column order,
hidden columns excluded — CSV and XLSX both."""

from __future__ import annotations

import io
import zipfile


ROWS = [["A", "B", "C"], ["1", "2", "3"], ["4", "5", "6"]]


def _csv_header(store, sid, spec=None):
    v = store.build_view(sid, {"source_id": sid, "filters": [], "sort": [], **(spec or {})})
    text = "".join(store.export_view_csv(v["view_id"]))
    return text.strip().splitlines()[0]


def test_csv_export_uses_saved_order_and_hides_hidden(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "l.csv"), name="l", build_fts=False)["id"]
    store.save_layout(sid, {"order": ["C", "A", "B"], "columns": {"B": {"hidden": True}}})
    assert _csv_header(store, sid) == "Line,Tags,Note,C,A"


def test_no_layout_means_storage_order(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "n.csv"), name="n", build_fts=False)["id"]
    assert _csv_header(store, sid) == "Line,Tags,Note,A,B,C"


def test_columns_the_layout_never_saw_still_export(store, write_csv):
    """A derived column added after the layout was saved must append, not
    silently vanish from the export."""
    sid = store.ingest_csv(write_csv(ROWS, "s.csv"), name="s", build_fts=False)["id"]
    store.save_layout(sid, {"order": ["C", "A", "B"], "columns": {}})
    res = store.add_derived_column(sid, "D", "A", "regex_extract", {"pattern": "(.)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _csv_header(store, sid) == "Line,Tags,Note,C,A,B,D"


def test_hide_everything_falls_back_to_all(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "h.csv"), name="h", build_fts=False)["id"]
    store.save_layout(sid, {"order": ["A", "B", "C"],
                            "columns": {n: {"hidden": True} for n in "ABC"}})
    assert _csv_header(store, sid) == "Line,Tags,Note,A,B,C"


def test_xlsx_export_honors_the_layout(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "x.csv"), name="x", build_fts=False)["id"]
    store.set_tags(sid, [1], store.list_tags()[0]["id"], True)
    store.save_layout(sid, {"order": ["B", "A", "C"], "columns": {"C": {"hidden": True}}})
    buf = store.export_tagged_xlsx()
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
        sheet = next(n for n in z.namelist() if "sheet" in n.lower())
        xml = z.read(sheet).decode()
    assert xml.index(">B<") < xml.index(">A<")
    assert ">C<" not in xml
