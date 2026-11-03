"""Loading a session re-imports a table that isn't open — the way it was
imported, from the file it was recorded against, or not at all.

A session names rows by rid, so re-importing is only safe if it produces
the same rows in the same order. It used to re-parse every missing source
as a default CSV: a headerless CSV gained a header and every tag slid one
row, a JSONL file came back as one column of JSON text, and the result
said `warnings: []`. And a session file is something another analyst
hands you — the path inside it is theirs, so nothing at that path is read
into the case unless it is the evidence the session describes.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from winnow import store as store_mod
from winnow.store import Store


@pytest.fixture
def case_b(tmp_path):
    s = Store(str(tmp_path / "b.db"))
    yield s
    s.close()


def _tagged_session(store, sid, rids):
    tag = store.upsert_tag(None, "Malware", "#ff0000", None)["id"]
    store.set_tags(sid, rids, tag, True)
    return store.export_case_session()


def _tagged_values(store, column):
    """{value of `column` on every tagged row} across the case's tables."""
    out = set()
    for src in store.list_sources():
        rids = [r[0] for r in store.db.execute(
            "SELECT rid FROM row_tags WHERE source_id=?", (src["id"],))]
        for rid in rids:
            out.add(store.db.execute(
                f'SELECT "{column}" FROM {src["table_name"]} WHERE rid=?', (rid,)).fetchone()[0])
    return out


def test_a_headerless_csv_comes_back_headerless(store, case_b, write_csv):
    path = write_csv([["evil.exe", "1"], ["benign.exe", "2"], ["calc.exe", "3"]], "noheader.csv")
    sid = store.ingest_csv(path, has_header=False, build_fts=False)["id"]
    session = _tagged_session(store, sid, [1])            # rid 1 is evil.exe

    res = case_b.import_case_session(session)
    assert res["sources_reimported"] == 1 and res["warnings"] == []
    assert _tagged_values(case_b, "col_1") == {"evil.exe"}


def test_a_jsonl_file_comes_back_as_json(store, case_b, tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps({"proc": p, "n": i}) + "\n"
                            for i, p in enumerate(["evil", "benign", "calc"])), encoding="utf-8")
    sid = store.ingest_json(str(path), build_fts=False)["id"]
    session = _tagged_session(store, sid, [1])

    res = case_b.import_case_session(session)
    assert res["sources_reimported"] == 1 and res["warnings"] == []
    assert [c["name"] for c in case_b.list_sources()[0]["columns"]] == ["proc", "n"]
    assert _tagged_values(case_b, "proc") == {"evil"}


def test_a_sqlite_table_comes_back_from_its_table(store, case_b, tmp_path):
    db = str(tmp_path / "History")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE urls (url TEXT, last_visit_time INTEGER)")
    con.executemany("INSERT INTO urls VALUES (?,?)",
                    [("https://a.example", 13350000000000000), ("https://evil.example", 13350000060000000)])
    con.commit()
    con.close()
    sid = store.ingest_sqlite_table(db, "urls", timestamp_columns=["last_visit_time"], build_fts=False)["id"]
    session = _tagged_session(store, sid, [2])

    res = case_b.import_case_session(session)
    assert res["sources_reimported"] == 1 and res["warnings"] == []
    assert _tagged_values(case_b, "url") == {"https://evil.example"}
    src = case_b.list_sources()[0]
    # The analyst-confirmed timestamp conversion is part of how it was read.
    assert case_b.db.execute(f"SELECT last_visit_time FROM {src['table_name']}").fetchone()[0] \
        == store.db.execute(f"SELECT last_visit_time FROM src_{sid}").fetchone()[0]


def test_a_file_that_is_not_the_recorded_one_is_not_imported(store, case_b, write_csv):
    path = write_csv([["proc"], ["evil.exe"], ["benign.exe"]], "ev.csv")
    sid = store.ingest_csv(path, build_fts=False)["id"]
    session = _tagged_session(store, sid, [1])
    write_csv([["proc"], ["benign.exe"], ["evil.exe"]], "ev.csv")   # same path, other evidence

    res = case_b.import_case_session(session)
    assert res["sources_reimported"] == 0 and res["tags_applied"] == 0
    assert case_b.list_sources() == []          # read, compared, never ingested
    assert len(res["warnings"]) == 1 and "not the file" in res["warnings"][0]


def test_an_old_session_is_only_applied_to_a_table_of_the_shape_it_recorded(store, case_b, write_csv):
    """A session saved before the ingest options were recorded can only be
    re-imported by guessing — and a wrong guess is caught by the shape the
    session did record, rather than tagging whichever rows moved into the
    tagged rids."""
    path = write_csv([["evil.exe", "1"], ["benign.exe", "2"], ["calc.exe", "3"]], "noheader.csv")
    sid = store.ingest_csv(path, has_header=False, build_fts=False)["id"]
    session = _tagged_session(store, sid, [1])
    for s in session["sources"]:
        s["source"].pop("ingest", None)

    res = case_b.import_case_session(session)
    assert res["sources_reimported"] == 0 and res["tags_applied"] == 0
    assert case_b.list_sources() == []          # the mis-parsed copy does not stay behind
    assert len(res["warnings"]) == 1 and "noheader.csv" in res["warnings"][0]


def test_an_open_table_of_another_shape_is_not_tagged(store, case_b, write_csv):
    """The same file (same hash) already open in this case, but imported
    with a header the session's copy didn't have: every rid is one row
    off, so nothing is applied."""
    path = write_csv([["evil.exe", "1"], ["benign.exe", "2"], ["calc.exe", "3"]], "noheader.csv")
    sid = store.ingest_csv(path, has_header=False, build_fts=False)["id"]
    session = _tagged_session(store, sid, [1])
    case_b.ingest_csv(path, build_fts=False)          # has_header=True

    res = case_b.import_case_session(session)
    assert res["tags_applied"] == 0 and res["sources_restored"] == 0
    assert case_b.db.execute("SELECT count(*) FROM row_tags").fetchone()[0] == 0
    assert len(res["warnings"]) == 1 and "noheader.csv" in res["warnings"][0]


@pytest.mark.parametrize("path", [r"\\attacker.example\share\ev.csv", "//attacker.example/share/ev.csv",
                                  r"\\?\UNC\attacker.example\share\ev.csv", "ev.csv"])
def test_a_network_or_relative_path_is_never_looked_at(case_b, monkeypatch, path):
    """On Windows, merely asking whether \\\\host\\share\\x exists makes
    an SMB connection that hands the host the analyst's NTLM credentials —
    and the path came out of a file somebody else wrote. A relative path
    would resolve against wherever Winnow happens to be running."""
    looked = []
    real_isfile, real_stat = os.path.isfile, os.stat
    monkeypatch.setattr(store_mod.os.path, "isfile", lambda p: looked.append(p) or real_isfile(p))
    monkeypatch.setattr(store_mod.os, "stat", lambda p, *a, **k: looked.append(p) or real_stat(p, *a, **k))
    session = {"format": "winnow-case-session/1", "sources": [{
        "source": {"name": "ev.csv", "path": path, "file_hash": "0" * 32, "row_count": 1,
                   "columns": [{"name": "a", "type": "text"}], "ingest": {"kind": "csv"}},
        "tag_defs": [], "row_tags": [], "row_notes": []}]}

    res = case_b.import_case_session(session)
    assert path not in looked
    assert res["sources_reimported"] == 0 and case_b.list_sources() == []
    assert len(res["warnings"]) == 1


def test_a_table_a_plugin_parsed_is_not_re_read_as_text(store, case_b, tmp_path):
    """ingest_rows has no reader to re-run — a plugin parsed the file. The
    old path read it with ingest_csv anyway: a $MFT as delimited text."""
    raw = tmp_path / "$MFT"
    raw.write_bytes(b"FILE0\x00\x01" * 64)
    sid = store.ingest_rows(["Entry", "Name"], [["0", "$MFT"], ["1", "$MFTMirr"]],
                            name="$MFT", path=str(raw), build_fts=False)["id"]
    session = _tagged_session(store, sid, [1])

    res = case_b.import_case_session(session)
    assert res["sources_reimported"] == 0 and case_b.list_sources() == []
    assert len(res["warnings"]) == 1 and "plugin" in res["warnings"][0]


def test_a_replace_load_leaves_a_table_it_could_not_apply_to_as_it_was(store, case_b, write_csv):
    """A replace clears the whole case (a table outside the snapshot must
    not keep its tags), but an open table the session refused to tag —
    its rows are not the session's — keeps the tags it has. Clearing them
    as well bought nothing: the session's tags for it were never going to
    be applied."""
    a_path = write_csv([["Image"], ["a.exe"], ["b.exe"]], "a.csv")
    nh_path = write_csv([["evil.exe", "1"], ["benign.exe", "2"], ["calc.exe", "3"]], "noheader.csv")
    a = store.ingest_csv(a_path, build_fts=False)["id"]
    nh = store.ingest_csv(nh_path, has_header=False, build_fts=False)["id"]
    tag = store.upsert_tag(None, "Malware", "#ff0000", None)["id"]
    store.set_tags(a, [1], tag, True)
    store.set_tags(nh, [1], tag, True)
    session = store.export_case_session()

    a_b = case_b.ingest_csv(a_path, build_fts=False)["id"]
    nh_b = case_b.ingest_csv(nh_path, build_fts=False)["id"]           # with a header: other rows
    later = case_b.ingest_csv(write_csv([["x"], ["1"]], "later.csv"), build_fts=False)["id"]
    mine = case_b.upsert_tag(None, "Reviewed", "#00ff00", None)["id"]
    case_b.set_tags(nh_b, [2], mine, True)
    case_b.set_tags(later, [1], mine, True)
    case_b.set_note(nh_b, 2, "checked by hand")

    res = case_b.import_case_session(session, merge=False)
    tagged = {(r[0], r[1]) for r in case_b.db.execute("SELECT source_id, rid FROM row_tags")}
    assert tagged == {(a_b, 1), (nh_b, 2)}
    assert [tuple(r) for r in case_b.db.execute("SELECT source_id, note FROM row_notes")] == [
        (nh_b, "checked by hand")]
    assert res["sources_restored"] == 1
    assert any("noheader.csv" in w and "as they were" in w for w in res["warnings"])


# ---------------------------------------------------------------- each reader
#
# One per reader the session can name. The CSV, JSON and SQLite readers are
# above; IIS and key=value are in test_iis_logs.py and test_kv_logs.py.

def _tagged_rows(store):
    """{table name: [cells of each tagged row, in rid order]} — the rows
    the tags ended up on, compared by content, since the two cases number
    their tables independently."""
    out = {}
    for src in store.list_sources():
        rids = [r[0] for r in store.db.execute(
            "SELECT rid FROM row_tags WHERE source_id=? ORDER BY rid", (src["id"],))]
        out[src["name"]] = [tuple(store.db.execute(
            f"SELECT * FROM {src['table_name']} WHERE rid=?", (rid,)).fetchone())[1:] for rid in rids]
    return out


def _without_reader(session):
    """The session as a build from before the reader was recorded saved it."""
    for s in session["sources"]:
        s["source"].pop("ingest", None)
    return session


def test_a_csv_comes_back_with_the_delimiter_and_types_it_was_imported_with(store, case_b, tmp_path):
    """The analyst picked ';' for a file the sniffer splits on ',', and kept
    the Pid column as text. A default read gives other columns."""
    path = tmp_path / "hosts.csv"
    path.write_text("Host,Domain;Pid\nWKS1,corp;4\nWKS2,corp;8\nWKS3,corp;12\n", encoding="utf-8")
    sid = store.ingest_csv(str(path), delimiter=";", column_types=["text", "text"], build_fts=False)["id"]
    session = _tagged_session(store, sid, [2])

    res = case_b.import_case_session(session)
    assert res["sources_reimported"] == 1 and res["warnings"] == []
    [src] = case_b.list_sources()
    assert [(c["name"], c["type"]) for c in src["columns"]] == [("Host,Domain", "text"), ("Pid", "text")]
    assert _tagged_rows(case_b) == {src["name"]: [("WKS2,corp", "8")]}


LOG_LINES = [
    "Host,User,Action",                                     # reads as a header to the CSV reader
    "Mar 14 08:00:01 wks1 sshd[311]: Accepted password for alice, port 22",
    "Mar 14 08:00:09 wks1 sudo: alice : TTY=pts/0 ; COMMAND=/bin/sh",
    "Mar 14 08:00:15 wks1 sshd[311]: Disconnected from user alice",
]


@pytest.mark.parametrize("recorded", [True, False], ids=["recorded", "guessed"])
def test_a_text_log_comes_back_one_line_to_a_row(store, case_b, tmp_path, recorded):
    """Nothing split and no line taken for a header — what the CSV reader
    would do to this file, which would move every tag one row. A session
    from before the reader was recorded guesses text for a .log."""
    path = tmp_path / "auth.log"
    path.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    sid = store.ingest_text(str(path), build_fts=False)["id"]
    session = _tagged_session(store, sid, [3])
    if not recorded:
        _without_reader(session)

    res = case_b.import_case_session(session)
    assert res["sources_reimported"] == 1 and res["warnings"] == []
    [src] = case_b.list_sources()
    assert [c["name"] for c in src["columns"]] == [store_mod.TEXT_COLUMN]
    assert _tagged_rows(case_b) == {src["name"]: [(LOG_LINES[2],)]}


@pytest.fixture
def workbook(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in {
        "Events": [["When", "Image"], ["2026-03-14 08:00:00", "svchost.exe"],
                   ["2026-03-14 08:01:00", "evil.exe"], ["2026-03-14 08:02:00", "calc.exe"]],
        "Logons": [["User", "Type"], ["alice", 10], ["bob", 3]],
    }.items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    path = tmp_path / "triage.xlsx"
    wb.save(path)
    return str(path)


def test_each_sheet_of_a_workbook_comes_back_from_its_own_sheet(store, case_b, workbook):
    """Every sheet of a workbook shares the file's hash, so the sheet is the
    only thing that says which rows a session's tags are for."""
    events = store.ingest_xlsx_sheet(workbook, "Events", build_fts=False)["id"]
    logons = store.ingest_xlsx_sheet(workbook, "Logons", build_fts=False)["id"]
    tag = store.upsert_tag(None, "Malware", "#ff0000", None)["id"]
    store.set_tags(events, [2], tag, True)
    store.set_tags(logons, [1], tag, True)
    session = store.export_case_session()

    res = case_b.import_case_session(session)
    assert res["sources_reimported"] == 2 and res["warnings"] == []
    assert _tagged_rows(case_b) == {"triage.Events": [("2026-03-14 08:01:00", "evil.exe")],
                                    "triage.Logons": [("alice", "10")]}


def test_a_workbook_from_an_old_session_is_not_guessed_at(store, case_b, workbook):
    """Which sheet isn't recorded, and a workbook has several: nothing is
    read, and the warning says to import it by hand."""
    sid = store.ingest_xlsx_sheet(workbook, "Events", build_fts=False)["id"]
    session = _without_reader(_tagged_session(store, sid, [2]))

    res = case_b.import_case_session(session)
    assert res["sources_reimported"] == 0 and case_b.list_sources() == []
    assert len(res["warnings"]) == 1 and "no record of how it was read" in res["warnings"][0]


@pytest.mark.parametrize("recorded", [True, False], ids=["recorded", "guessed"])
@pytest.mark.parametrize("era", ["old", "new"])
def test_a_plaso_file_comes_back_as_its_timeline(store, case_b, tmp_path, era, recorded):
    from test_plaso_import import make_new_plaso, make_old_plaso

    path = tmp_path / "case.plaso"
    (make_old_plaso if era == "old" else make_new_plaso)(str(path))
    sid = store.ingest_plaso(str(path), build_fts=False)["id"]
    session = _tagged_session(store, sid, [1])
    want = _tagged_rows(store)
    if not recorded:
        _without_reader(session)

    res = case_b.import_case_session(session)
    assert res["sources_reimported"] == 1 and res["warnings"] == []
    assert _tagged_rows(case_b) == want


def test_a_session_that_names_no_file_re_imports_nothing(store, case_b, write_csv):
    path = write_csv([["proc"], ["evil.exe"], ["benign.exe"]], "ev.csv")
    sid = store.ingest_csv(path, build_fts=False)["id"]
    session = _tagged_session(store, sid, [1])
    session["sources"][0]["source"].pop("path")

    res = case_b.import_case_session(session)
    assert res["sources_reimported"] == 0 and res["tags_applied"] == 0
    assert case_b.list_sources() == []
    assert res["warnings"] == ["'ev.csv' isn't in this case and the session doesn't say which file it came "
                               "from — skipped"]


def test_a_file_gone_from_its_path_is_skipped_and_named(store, case_b, write_csv):
    path = write_csv([["proc"], ["evil.exe"], ["benign.exe"]], "ev.csv")
    sid = store.ingest_csv(path, build_fts=False)["id"]
    session = _tagged_session(store, sid, [1])
    os.remove(path)

    res = case_b.import_case_session(session)
    assert res["sources_reimported"] == 0 and case_b.list_sources() == []
    assert res["warnings"] == [f"'ev.csv' isn't in this case and its original file wasn't found at {path} "
                               "— skipped"]


def test_a_file_that_changes_while_it_is_read_is_removed_again(store, case_b, write_csv, monkeypatch):
    """The file is checked against the session before it is read; if it is
    replaced between the two, the copy that was read is not the session's
    evidence, and is dropped rather than tagged."""
    path = write_csv([["proc"], ["evil.exe"], ["benign.exe"]], "ev.csv")
    sid = store.ingest_csv(path, build_fts=False)["id"]
    session = _tagged_session(store, sid, [1])
    real = case_b.ingest_csv

    def replaced_first(p, *a, **k):
        write_csv([["proc"], ["benign.exe"], ["evil.exe"]], "ev.csv")
        return real(p, *a, **k)
    monkeypatch.setattr(case_b, "ingest_csv", replaced_first)

    res = case_b.import_case_session(session)
    assert res["sources_reimported"] == 0 and res["tags_applied"] == 0
    assert case_b.list_sources() == []
    assert res["warnings"] == ["'ev.csv' changed on disk while it was being re-imported — removed again"]
