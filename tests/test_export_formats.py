"""Exporting the current view in every format Case ▾ → Export… offers.

CSV was the only format for a long time, and everything the CSV export
had to get right (the view's filter, sort and search; the layout's column
order with hidden columns left out; Tags and Note; Table on a merge;
tagged rows only) still has to hold for TSV, Excel, JSON Lines, JSON,
Markdown and HTML. The test that matters most here reads every format
back into rows and holds them to the CSV of the same view: the formats
share one reader (Store._export_view_rows) precisely so they cannot
disagree, and a writer that dropped a column, put a tag on the wrong row
or lost a line break would hand an analyst a different answer depending
on which button they pressed.

The other half is what each format does with a value. The three a
spreadsheet opens (CSV, TSV, Excel) put the formula guard's apostrophe in
front of =, +, -, @; JSON, Markdown and HTML keep every value exactly, and
Markdown and HTML escape what would otherwise turn evidence text into
formatting or markup.
"""

from __future__ import annotations

import csv
import io
import json
import re
from html.parser import HTMLParser

import pytest
from openpyxl import load_workbook

from winnow import store as store_mod
from winnow.store import VIEW_EXPORT_FORMATS, _md_cell

TEXT_FORMATS = ["csv", "tsv", "jsonl", "json", "md", "html"]
GUARDED = {"csv", "tsv", "xlsx"}


# ----------------------------------------------------------------- readers

def _md_unescape(cell: str) -> str:
    cell = re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", cell.strip())
    return cell.replace("<br>", "\n")


def _md_rows(text: str) -> list[list[str]]:
    lines = text.splitlines()
    assert re.fullmatch(r"\| ---: (\| --- )*\|", lines[1]), lines[1]
    out = []
    for line in [lines[0], *lines[2:]]:
        assert line.startswith("| ") and line.endswith(" |"), line
        out.append([_md_unescape(c) for c in re.split(r"(?<!\\)\|", line[1:-1])])
    return out


class _Table(HTMLParser):
    """The page's table as rows of cell text, a Tags cell's chips joined
    with "; " as the other formats write them."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.cell: list[str] | None = None
        self.chips: list[str] | None = None
        self.in_chip = False
        self.about = None
        self._about = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            self.cell, self.chips = [], []
        elif tag == "span" and a.get("class") == "tag":
            self.in_chip = True
            self.chips.append("")
        elif tag == "p" and a.get("class") == "about":
            self._about = True
            self.about = ""

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.rows[-1].append("; ".join(self.chips) if self.chips else "".join(self.cell))
            self.cell = self.chips = None
        elif tag == "span":
            self.in_chip = False
        elif tag == "p":
            self._about = False

    def handle_data(self, data):
        if self._about:
            self.about += data
        elif self.in_chip:
            self.chips[-1] += data
        elif self.cell is not None:
            self.cell.append(data)


def _html_rows(text: str) -> list[list[str]]:
    t = _Table()
    t.feed(text)
    return t.rows


def _json_rows(objs: list[dict]) -> list[list[str]]:
    if not objs:
        return []
    keys = list(objs[0])
    out = [keys]
    for o in objs:
        assert list(o) == keys, "every object has the same keys, in the same order"
        out.append([str(o["Line"]) if k == "Line" else "; ".join(o[k]) if k == "Tags"
                    else "" if o[k] is None else str(o[k]) for k in keys])
    return out


def _read(store, view_id, fmt, tmp_path, tagged_only=False, **kw) -> list[list[str]]:
    """The export of `fmt` as rows of text, header first — every format
    read back into the CSV's shape so they can be compared cell for cell."""
    if fmt == "xlsx":
        path = tmp_path / "v.xlsx"
        store.export_view_xlsx(view_id, str(path), tagged_only)
        wb = load_workbook(path)
        rows = [r for ws in wb.worksheets for r in ws.iter_rows(values_only=True)]
        return [["" if v is None else str(v) for v in r] for r in rows]
    text = "".join(store.export_view(view_id, fmt, tagged_only, **kw))
    if fmt == "csv":
        return list(csv.reader(io.StringIO(text)))
    if fmt == "tsv":
        return list(csv.reader(io.StringIO(text), delimiter="\t"))
    if fmt == "jsonl":
        return _json_rows([json.loads(line) for line in text.splitlines()])
    if fmt == "json":
        return _json_rows(json.loads(text))
    if fmt == "md":
        return _md_rows(text)
    return _html_rows(text)


def _unguarded(rows):
    return [[c[1:] if c[:1] == "'" and c[1:2] in "=+-@\t\r" else c for c in r] for r in rows]


# ------------------------------------------------------------------ fixture

@pytest.fixture
def case(store, write_csv):
    """A table shaped to trip each format: its own Note column (a JSON key
    collision), a column the layout hides, cells that are formulas to a
    spreadsheet, Markdown and HTML, a line break and a tab, two tags on one
    row (one whose name is a formula), and notes."""
    rows = [
        ["Host", "CommandLine", "Note", "Secret", "Size"],
        ["WKSTN-1", "=cmd|' /C calc'!A1", "own note", "hide me", "12"],
        ["SRV-02", "whoami", "", "hide me", "-5"],
        ["WKSTN-3", "powershell -c \"Get-Item C:\\Windows\\*\" | Out-File <x>&$y", "", "hide me", "7"],
        ["WKSTN-4", "line one\nline\ttwo", "@SUM(1)", "hide me", "1e3"],
        ["WKSTN-5", "**bold** _it_ `code` [link](http://x) ~~s~~", "", "hide me", ""],
    ]
    sid = store.ingest_csv(write_csv(rows, "fmt.csv"), name="fmt.csv", build_fts=False)["id"]
    store.save_layout(sid, {"order": ["Host", "CommandLine", "Note", "Secret", "Size"],
                            "columns": {"Secret": {"hidden": True}}})
    keep = store.upsert_tag(None, "Keep", "#ff0000", None)["id"]
    odd = store.upsert_tag(None, "@odd", "#00aa00", None)["id"]
    store.set_tags(sid, [1, 3], keep, True)
    store.set_tags(sid, [3], odd, True)
    store.set_note(sid, 4, 'saw "this"\nsecond line')
    store.set_note(sid, 1, "=not a formula")
    return sid


def _view(store, sid):
    """Filtered (no SRV row) and sorted descending by Host, so a format that
    read storage order or ignored the filter shows up as a different list."""
    return store.build_view(sid, {"filters": [{"column": "Host", "op": "contains", "value": "WKSTN"}],
                                  "sort": [{"column": "Host", "dir": "desc"}]})["view_id"]


# -------------------------------------------------------------------- tests

@pytest.mark.parametrize("fmt", [f for f in VIEW_EXPORT_FORMATS if f != "csv"])
@pytest.mark.parametrize("tagged_only", [False, True])
def test_every_format_holds_the_same_rows_as_the_csv(store, case, tmp_path, fmt, tagged_only):
    vid = _view(store, case)
    ref = _read(store, vid, "csv", tmp_path, tagged_only)
    assert [r[0] for r in ref[1:]] == (["3", "1"] if tagged_only else ["5", "4", "3", "1"])
    assert "Secret" not in ref[0]
    got = _read(store, vid, fmt, tmp_path, tagged_only)
    if fmt in ("jsonl", "json"):
        # JSON keys are unique, so the table's own Note column is Note_1.
        assert got[0] == ["Line", "Tags", "Note", "Host", "CommandLine", "Note_1", "Size"]
        got[0][5] = "Note"
    if fmt in GUARDED:
        assert got == ref
    else:
        assert got == _unguarded(ref)


@pytest.mark.parametrize("fmt", list(VIEW_EXPORT_FORMATS))
def test_the_formula_guard_is_on_exactly_the_spreadsheet_formats(store, case, tmp_path, fmt):
    rows = _read(store, _view(store, case), fmt, tmp_path)
    line1 = next(r for r in rows if r[0] == "1")
    cmd = line1[4]
    if fmt in GUARDED:
        assert cmd == "'=cmd|' /C calc'!A1"
        assert line1[2] == "'=not a formula"
    else:
        assert cmd == "=cmd|' /C calc'!A1"
        assert line1[2] == "=not a formula"
    # The stored value is never touched, whichever format went out.
    page = store.fetch_rows(store.build_view(case, {})["view_id"], 0, 1)["rows"]
    assert page[0]["cells"][1] == "=cmd|' /C calc'!A1"


def test_a_merge_says_which_table_each_row_came_from_in_every_format(store, write_csv, tmp_path):
    a = store.ingest_csv(write_csv([["user"], ["alice"], ["bob"]], name="a.csv"), name="hostA.csv",
                         build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["user"], ["carol"]], name="b.csv"), name="hostB.csv",
                         build_fts=False)["id"]
    store.set_source_nickname(b, "Host B")
    mid = store.create_merge("M", [a, b])["id"]
    vid = store.build_view(mid, {"sort": [{"column": "user", "dir": "asc"}]})["view_id"]
    for fmt in VIEW_EXPORT_FORMATS:
        rows = _read(store, vid, fmt, tmp_path)
        assert rows[0] == ["Line", "Table", "Tags", "Note", "user"], fmt
        assert [r[:2] + r[4:] for r in rows[1:]] == [["1", "hostA.csv", "alice"], ["2", "hostA.csv", "bob"],
                                                     ["1", "Host B", "carol"]], fmt


@pytest.mark.parametrize("fmt", list(VIEW_EXPORT_FORMATS))
def test_all_three_ways_of_reading_a_view_agree(store, case, tmp_path, fmt):
    """An unfiltered, unsorted view is read straight off the table
    (root_virtual), a small group of it straight off the table too
    (group_virtual), and anything else off v.view_N. Each must give every
    format the same rows the materialised read does."""
    # One at a time: a new view of a table evicts the one before it.
    root_id = store.build_view(case, {})["view_id"]
    assert store._views[root_id]["kind"] == "root_virtual"
    root = _read(store, root_id, fmt, tmp_path)
    by_line = _read(store, store.build_view(case, {"sort": [{"column": "__line__", "dir": "asc"}]})["view_id"],
                    fmt, tmp_path)
    assert root == by_line
    assert [r[0] for r in root[1:]] == ["1", "2", "3", "4", "5"]
    group = store.expand_group(store.build_view(case, {})["view_id"], "Note", "")["view_id"]
    assert store._views[group]["kind"] == "group_virtual"
    rows = _read(store, group, fmt, tmp_path)
    assert rows[0] == root[0]
    assert rows[1:] == [r for r in root[1:] if r[5] == ""]


def test_json_writes_values_as_values(store, case):
    vid = _view(store, case)
    objs = json.loads("".join(store.export_view(vid, "json")))
    by_line = {o["Line"]: o for o in objs}
    assert by_line[3]["Tags"] == ["Keep", "@odd"]
    assert by_line[5]["Tags"] == [] and by_line[5]["Note"] is None
    assert by_line[4]["Note"] == 'saw "this"\nsecond line'
    assert by_line[4]["CommandLine"] == "line one\nline\ttwo"
    assert by_line[1]["Note_1"] == "own note" and by_line[1]["Note"] == "=not a formula"
    lines = "".join(store.export_view(vid, "jsonl")).splitlines()
    assert [json.loads(x) for x in lines] == objs


def test_an_empty_view_is_still_a_valid_file(store, case, tmp_path):
    vid = store.build_view(case, {"filters": [{"column": "Host", "op": "contains", "value": "nothing"}]})["view_id"]
    assert json.loads("".join(store.export_view(vid, "json"))) == []
    assert "".join(store.export_view(vid, "jsonl")) == ""
    assert len(_md_rows("".join(store.export_view(vid, "md")))) == 1
    page = "".join(store.export_view(vid, "html"))
    assert page.rstrip().endswith("</html>") and "0 rows" in page
    assert _read(store, vid, "xlsx", tmp_path)[0][:3] == ["Line", "Tags", "Note"]


def test_markdown_escapes_what_it_would_otherwise_render():
    assert _md_cell("a|b") == r"a\|b"
    assert _md_cell("**x** _y_ `z` <b> & $ ~") == r"\*\*x\*\* \_y\_ \`z\` \<b\> \& \$ \~"
    # A backslash is doubled only where it would escape what follows, so
    # a Windows path reads as itself in the file.
    assert _md_cell(r"C:\Windows\System32") == r"C:\Windows\System32"
    assert _md_cell(r"\\srv\share") == r"\\\srv\share"
    assert _md_cell("a\r\nb\nc\rd") == "a<br>b<br>c<br>d"
    assert _md_cell("bell\x07") == r"bell\x07"
    assert _md_cell(None) == "" and _md_cell(12) == "12"


def test_html_escapes_every_value_and_runs_nothing(store, write_csv):
    sid = store.ingest_csv(write_csv([["<b>col</b>"], ["<script>alert(1)</script>"], ['" onmouseover="x']],
                                     name="x.csv"), name="<i>x</i>.csv", build_fts=False)["id"]
    good = store.upsert_tag(None, "<Keep>", "#e5484d", None)["id"]
    # A colour arrives in a session someone else sent as easily as a name.
    bad = store.upsert_tag(None, "Beacon", "red; background:url(http://evil.example/)", None)["id"]
    store.set_tags(sid, [1], good, True)
    store.set_tags(sid, [1], bad, True)
    vid = store.build_view(sid, {})["view_id"]
    page = "".join(store.export_view(vid, "html", about='Narrowed by <x> & "y"'))
    assert "<script" not in page and "<b>" not in page and "<i>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<title>&lt;i&gt;x&lt;/i&gt;.csv</title>" in page
    assert 'style="--c:#e5484d"' in page
    assert "evil.example" not in page and "url(" not in page
    t = _Table()
    t.feed(page)
    assert t.about == 'Narrowed by <x> & "y"'
    assert t.rows[1][1] == "<Keep>; Beacon"
    assert t.rows[2][3] == '" onmouseover="x'
    assert re.search(r'<p class="meta foot">2 rows</p>', page)


def test_html_says_which_rows_and_when(store, case):
    vid = _view(store, case)
    every = "".join(store.export_view(vid, "html"))
    tagged = "".join(store.export_view(vid, "html", True))
    assert "Every row of the view · exported " in every and "4 rows" in every
    assert "Tagged rows of the view · exported " in tagged and "2 rows" in tagged
    assert '<p class="about">' not in every   # nothing to say, no empty line


def test_xlsx_names_its_sheet_after_the_table_and_continues_past_a_full_one(store, case, tmp_path, monkeypatch):
    store.set_source_nickname(case, "Host WKSTN [triage]")
    monkeypatch.setattr(store_mod, "XLSX_MAX_ROWS", 3)   # two rows a sheet, plus the header
    path = tmp_path / "v.xlsx"
    stats = store.export_view_xlsx(_view(store, case), str(path))
    assert stats == {"rows": 4, "sheets": 2}
    wb = load_workbook(path)
    assert wb.sheetnames == ["Host WKSTN _triage_", "Host WKSTN _triage_ (2)"]
    assert [c.value for c in wb.worksheets[1][1]][:3] == ["Line", "Tags", "Note"]
    assert [r[0] for ws in wb.worksheets for r in ws.iter_rows(min_row=2, values_only=True)] == [5, 4, 3, 1]


def test_an_unknown_format_is_refused(store, case):
    with pytest.raises(ValueError):
        store.export_view(_view(store, case), "pdf")


# ------------------------------------------------------------------- route

@pytest.mark.parametrize("fmt", list(VIEW_EXPORT_FORMATS))
def test_the_route_names_the_file_and_its_type(store, client, case, fmt):
    label, media, ext = VIEW_EXPORT_FORMATS[fmt]
    r = client.get("/api/export", params={"view_id": _view(store, case), "format": fmt})
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0] == media
    assert f'filename="timeline-export.{ext}"' in r.headers["content-disposition"]
    assert r.headers["content-disposition"].startswith("attachment")
    named = client.get("/api/export", params={"view_id": _view(store, case), "format": fmt,
                                              "filename": f"Security – triage.{ext}"})
    assert "filename*=UTF-8''Security%20%E2%80%93%20triage." + ext in named.headers["content-disposition"]


def test_the_route_still_means_csv_when_no_format_is_named(store, client, case):
    vid = _view(store, case)
    r = client.get("/api/export", params={"view_id": vid})
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text == "".join(store.export_view_csv(vid))


def test_the_route_refuses_an_unknown_format_and_an_expired_view(store, client, case):
    r = client.get("/api/export", params={"view_id": _view(store, case), "format": "pdf"})
    assert r.status_code == 400 and "pdf" in r.json()["detail"]
    for fmt in VIEW_EXPORT_FORMATS:
        assert client.get("/api/export", params={"view_id": "v_gone", "format": fmt}).status_code == 409, fmt


def test_the_log_counts_rows_not_lines(store, client, case):
    """A note and a cell both hold a line break; counting newlines said
    more rows than went out, and for JSON or HTML said nothing useful."""
    from winnow import log as wlog
    vid = _view(store, case)
    for fmt in VIEW_EXPORT_FORMATS:
        client.get("/api/export", params={"view_id": vid, "format": fmt})
    done = [e["message"] for e in wlog.entries() if e["message"].startswith("Export finished")]
    for fmt, (label, _, _) in VIEW_EXPORT_FORMATS.items():
        line = next(m for m in done if m.startswith(f"Export finished: {label} view {vid}"))
        assert " — 4 rows" in line, line
