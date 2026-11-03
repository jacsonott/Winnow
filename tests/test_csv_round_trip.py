"""A CSV's awkward cells survive the trip out of Winnow and back in.

Ingest a file, export it through GET /api/export (what the browser
downloads), and ingest the export: every cell comes back as it went out.
Each reader and the exporter are tested on their own elsewhere; what only a
round trip shows is the two disagreeing about a cell, which is a value an
analyst hands to someone else changed without anyone noticing.

The one difference allowed is deliberate. A cell that opens with = + - @,
a tab or a CR is written with a leading apostrophe so a spreadsheet can't
run it as a formula (_csv_safe), so it comes back with one.
"""

from __future__ import annotations

import csv
import io
import sys

import pytest

from winnow import store as store_mod

HEADER = ["When", "Host", "Value", "Имя"]

# (label, cell) — each one lands in the Value column of its own row.
AWKWARD = [
    ("plain", "cmd.exe /c whoami"),
    ("embedded quotes", 'cmd.exe /c "echo ""hi"""'),
    ("only a quote", '"'),
    ("newline", "line one\nline two"),
    ("CRLF inside", "line one\r\nline two"),
    ("trailing newline", "ends with a newline\n"),
    ("commas and tabs", "a,b;c\td"),
    ("NUL", "ab\x00cd"),
    ("long", "A" * 200_000),   # past csv's default 131,072-character field limit
    ("Cyrillic", "Привет, мир"),
    ("CJK", "日本語のテキスト"),
    ("emoji", "emoji 🔥👀 and a ZWJ 👩‍💻"),
    ("Arabic", "مرحبا بالعالم"),
    ("combining", "e\u0301 vs \u00e9"),
    ("padded", "  padded  "),
    ("blank", ""),
    ("whitespace", "   "),
    ("backslashes", "C:\\Windows\\System32\\"),
    ("formula", "=HYPERLINK(\"http://x\",\"y\")"),
    ("plus", "+1"),
    ("minus", "-2 /c calc"),
    ("at", "@SUM(A1)"),
    ("tab first", "\tindented"),
    ("CR first", "\rreturn first"),
    ("apostrophe first", "'already quoted"),
]


def _source_file(path):
    """The awkward rows as a CSV with a UTF-8 byte-order mark, as Excel
    writes one. Every field quoted by hand, not by csv.writer: Python
    3.10's refuses to write a NUL at all."""
    rows = [HEADER] + [[f"2026-03-14 08:00:{i:02d}", label, cell, "ключ"]
                       for i, (label, cell) in enumerate(AWKWARD)]
    text = "".join(",".join('"' + c.replace('"', '""') + '"' for c in row) + "\r\n" for row in rows)
    path.write_bytes(text.encode("utf-8-sig"))
    return path


def _all_rows(store, sid):
    view = store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})
    src = store.get_source(sid)
    return [c["name"] for c in src["columns"]], [
        r["cells"] for r in store.fetch_rows(view["view_id"], 0, src["row_count"] + 1)["rows"]]


def _guarded(cell):
    return "'" + cell if cell and cell[0] in store_mod._CSV_FORMULA_LEAD else cell


@pytest.fixture
def round_trip(store, client, tmp_path):
    """Ingest the awkward file, export it over HTTP, ingest the export.
    Returns (columns, rows) of the original and of the copy."""
    first = store.ingest_csv(str(_source_file(tmp_path / "awkward.csv")), name="awkward.csv", build_fts=False)
    view = store.build_view(first["id"], {"source_id": first["id"], "filters": [], "sort": []})
    res = client.get("/api/export", params={"view_id": view["view_id"]})
    assert res.status_code == 200, res.text
    exported = tmp_path / "exported.csv"
    exported.write_bytes(res.content)
    second = store.ingest_csv(str(exported), name="exported.csv", build_fts=False)
    return _all_rows(store, first["id"]), _all_rows(store, second["id"])


def test_the_import_reads_every_awkward_cell_as_written(round_trip):
    (cols, rows), _ = round_trip
    # The byte-order mark is the file's encoding, not part of a name.
    assert cols == HEADER
    assert [r[2] for r in rows] == [cell for _, cell in AWKWARD]


def test_every_cell_comes_back_from_an_export_as_it_went_out(round_trip):
    (cols, rows), (back_cols, back) = round_trip
    assert back_cols == ["Line", "Tags", "Note", *cols]
    assert len(back) == len(rows)
    wrong = []
    for (label, _), row, got in zip(AWKWARD, rows, back):
        want = [_guarded(c) for c in row]
        if got[3:] != want:
            wrong.append(f"{label}: sent {want!r:.120}, got back {got[3:]!r:.120}")
    assert not wrong, "cells changed on the way out and back:\n  " + "\n  ".join(wrong)
    assert [r[0] for r in back] == [str(i) for i in range(1, len(rows) + 1)]


def test_the_formula_guard_is_the_only_change(round_trip):
    """Pinned separately so a change to the guard reads as that, not as
    the round trip breaking: the guarded cells, and only those, gain one
    leading apostrophe — and a cell that already had one keeps exactly it."""
    (_, rows), (_, back) = round_trip
    changed = {label for (label, _), row, got in zip(AWKWARD, rows, back) if got[3:] != row}
    assert changed == {"formula", "plus", "minus", "at", "tab first", "CR first"}
    already = next(got for (label, _), got in zip(AWKWARD, back) if label == "apostrophe first")
    assert already[5] == "'already quoted"


def test_tags_and_notes_make_the_trip_too(store, client, tmp_path):
    """The analyst's own columns: a tag name and a note with a newline,
    quotes and a formula lead in it."""
    first = store.ingest_csv(str(_source_file(tmp_path / "awkward.csv")), name="awkward.csv", build_fts=False)
    sid = first["id"]
    tag = store.upsert_tag(None, "Needs \"review\", now", "#cc3333", None)
    store.set_tags(sid, [2, 5], tag["id"], True)
    note = "=not a formula\nsecond line, with \"quotes\""
    store.set_note(sid, 5, note)
    view = store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})
    res = client.get("/api/export", params={"view_id": view["view_id"], "tagged_only": "true"})
    assert res.status_code == 200, res.text
    exported = tmp_path / "tagged.csv"
    exported.write_bytes(res.content)
    back_cols, back = _all_rows(store, store.ingest_csv(str(exported), name="tagged.csv", build_fts=False)["id"])
    assert back_cols[:3] == ["Line", "Tags", "Note"]
    assert [(r[0], r[1], r[2]) for r in back] == [
        ("2", 'Needs "review", now', ""),
        ("5", 'Needs "review", now', "'" + note),
    ]


ESC, MARK = store_mod._NUL_ESC, store_mod._NUL_MARK


@pytest.mark.parametrize("row, text", [
    (["ab\x00cd", "x"], "ab\x00cd,x\n"),
    (["\x00"], "\x00\n"),
    (['"quoted\x00"', "two\nlines\x00"], '"""quoted\x00""","two\nlines\x00"\n'),
    # The escape's own characters come out as themselves.
    ([f"{ESC}{MARK}", f"{ESC}\x00"], f"{ESC}{MARK},{ESC}\x00\n"),
    ([7, None, "a\x00"], "7,,a\x00\n"),
    # A bare CR is quoted, or a reader splits the row there.
    (["'\rreturn first", "a\rb", "x"], '"\'\rreturn first","a\rb",x\n'),
    (["a\r\nb", "a\nb"], '"a\r\nb","a\nb"\n'),
    ([""], '""\n'),
])
def test_the_export_writes_every_cell_as_3_11_does_on_every_python(monkeypatch, row, text):
    """Python 3.10's csv writer refuses a NUL ("need to escape"), which cut
    an export off at the first row holding one, and leaves a field with a
    bare CR unquoted, which splits its row for whoever reads the file. The
    way round both is forced here so every CI Python runs it, and on 3.11+
    checked against what csv itself writes, so the expected text is csv's
    answer, not this test's."""
    monkeypatch.setattr(store_mod, "_CSV_WRITES_ANY_CELL", False)
    buf = io.StringIO()
    w = store_mod._CsvWriter(buf)
    w.writerow(["head"])
    w.writerow(row)
    assert buf.getvalue() == "head\n" + text
    if sys.version_info >= (3, 11):
        native = io.StringIO()
        csv.writer(native, lineterminator="\n").writerows([["head"], row])
        assert buf.getvalue() == native.getvalue()
