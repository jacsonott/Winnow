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
