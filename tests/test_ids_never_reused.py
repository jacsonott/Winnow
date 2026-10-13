"""Ids a case file hands out are never handed out again.

`sources.id`, `tag_defs.id`, `merges.id` and `sql_tabs.id` were plain
INTEGER PRIMARY KEYs, so deleting the newest row gave its id to the next
insert — and every one of those ids is held somewhere that outlives the
row: a view handle or a tag list in another window, an undo entry, an
indicator's auto-tag, a dashboard widget's `src_N`. With the id reused,
"this no longer exists" became "this is now a different table": a stale
window tagged rows of the next import, Ctrl+Z tagged rows of a file nobody
had touched, a deleted tag's auto-tag matches turned up as "Benign".

The schema is AUTOINCREMENT now, and a case file created before that is
rebuilt on open with its counters seeded past every id it still mentions.
The migration tests build the old file from the old DDL directly — opening
it with a Store first would migrate it before the test could look.
"""

from __future__ import annotations

import json
import sqlite3

from winnow.store import META_SCHEMA, Store


def test_the_newest_table_s_id_is_not_given_to_the_next_import(store, write_csv):
    a = store.ingest_csv(write_csv([["x"], ["1"]], "a.csv"), build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["x"], ["2"]], "b.csv"), build_fts=False)["id"]
    store.drop_source(b)
    c = store.ingest_csv(write_csv([["x"], ["3"]], "c.csv"), build_fts=False)["id"]
    assert c not in (a, b)


def test_the_newest_tag_s_id_is_not_given_to_the_next_tag(store):
    t = store.upsert_tag(None, "Lateral", "#ff0000", None)["id"]
    store.delete_tag(t)
    assert store.upsert_tag(None, "Benign2", "#00ff00", None)["id"] != t


def test_merge_and_sql_tab_ids_are_not_reused(store, write_csv):
    a = store.ingest_csv(write_csv([["x"], ["1"]], "a.csv"), build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["x"], ["2"]], "b.csv"), build_fts=False)["id"]
    m = store.create_merge("AB", [a, b])["id"]   # signed: -merges.id
    store.delete_merge(-m)
    assert store.create_merge("AB again", [a, b])["id"] != m

    tab = store.create_sql_tab("Query 1")["id"]
    store.delete_sql_tab(tab)
    assert store.create_sql_tab("Query 2")["id"] != tab


def _old_case_file(path: str, *, oldest_sources: bool = False) -> None:
    """A case file the way the previous schema wrote it: the same
    META_SCHEMA minus AUTOINCREMENT (so no sqlite_sequence at all).
    `oldest_sources` goes one step further back, to a sources table from
    before nicknames and origins, which Store.__init__ ALTERs in first."""
    con = sqlite3.connect(path)
    if oldest_sources:
        con.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT NOT NULL, path TEXT, "
                    "table_name TEXT NOT NULL, row_count INTEGER NOT NULL DEFAULT 0, "
                    "columns TEXT NOT NULL, file_hash TEXT, imported_at TEXT, "
                    "has_fts INTEGER NOT NULL DEFAULT 0)")
    con.executescript(META_SCHEMA.replace(" AUTOINCREMENT", ""))
    cols = json.dumps([{"name": "x", "type": "TEXT"}])
    for sid in (1, 2):
        con.execute(f"CREATE TABLE src_{sid} (rid INTEGER PRIMARY KEY, x TEXT)")
        con.executemany(f"INSERT INTO src_{sid}(x) VALUES (?)", [(f"{sid}-a",), (f"{sid}-b",)])
        con.execute("INSERT INTO sources(id, name, table_name, row_count, columns) VALUES (?,?,?,2,?)",
                    (sid, f"f{sid}.csv", f"src_{sid}", cols))
    con.executemany("INSERT INTO tag_defs(id, name, color) VALUES (?,?,?)",
                    [(1, "TA", "#c0392b"), (2, "Suspicious", "#d68a2e")])
    con.execute("INSERT INTO row_tags VALUES (1, 1, 2)")            # a real assignment
    # What the reuse bugs left behind in files in the wild: assignments for
    # a deleted tag (6) and a deleted table (5), an indicator still naming
    # the dead tag, and a widget written against a table deleted since (9).
    con.execute("INSERT INTO row_tags VALUES (2, 1, 6)")
    con.execute("INSERT INTO row_tags VALUES (5, 1, 1)")
    con.execute("INSERT INTO row_notes VALUES (5, 1, 'ghost')")
    con.execute("INSERT INTO watchlist(value, auto_tag_id) VALUES ('mimikatz', 6)")
    con.execute("INSERT INTO dashboards(name, widgets) VALUES ('Board', ?)",
                (json.dumps([{"sql": "SELECT count(*) FROM src_9"}]),))
    con.execute("INSERT INTO sql_tabs(id, name, sql) VALUES (3, 'Query 3', '')")
    con.execute("INSERT INTO merges(id, name, source_ids) VALUES (4, 'M', '[1, 2]')")
    con.commit()
    con.close()


def test_an_old_case_file_skips_every_id_it_still_mentions(tmp_path, write_csv):
    path = str(tmp_path / "old.db")
    _old_case_file(path)
    s = Store(path)
    try:
        # The ids still named anywhere are never handed out: not 3 (MAX(id)
        # + 1 on sources) but past the widget's src_9; not 3 on tags but
        # past the orphaned 6.
        sid = s.ingest_csv(write_csv([["x"], ["new"]], "n.csv"), build_fts=False)["id"]
        assert sid > 9
        assert s.upsert_tag(None, "New tag", "#000000", None)["id"] > 6
        assert s.create_sql_tab("Query")["id"] > 3
        assert -s.create_merge("Again", [1, 2])["id"] > 4   # a merge reads back signed

        # The rows survived the rebuild, ids and all, and still read.
        assert [x["id"] for x in s.list_sources()] == [1, 2, sid]
        assert s.fetch_rows(s.build_view(1, {})["view_id"], 0, 10)["rows"][0]
        assert {(t["id"], t["name"]) for t in s.list_tags()} >= {(1, "TA"), (2, "Suspicious")}
        assert [tuple(r) for r in s.db.execute("SELECT source_id, rid, tag_id FROM row_tags")] == [(1, 1, 2)]

        # What reuse had already left behind is gone rather than waiting
        # for an id that will never come to claim it.
        assert s.db.execute("SELECT count(*) FROM row_notes").fetchone()[0] == 0
        assert s.list_indicators()[0]["auto_tag_id"] is None
        assert s.list_sources()[1]["tagged_row_count"] == 0
    finally:
        s.close()

    # Once, not on every open: the second open finds AUTOINCREMENT already
    # there and the counters where the first left them.
    s = Store(path)
    try:
        for table in ("sources", "tag_defs", "merges", "sql_tabs"):
            ddl = s.db.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            assert "AUTOINCREMENT" in ddl
        assert s.ingest_csv(write_csv([["x"], ["again"]], "m.csv"), build_fts=False)["id"] == sid + 1
    finally:
        s.close()


def test_an_older_sources_table_keeps_the_columns_added_to_it(tmp_path, write_csv):
    """A sources table that only gained nickname/origin/origin_meta by
    ALTER TABLE on this same open keeps them through the rebuild, which
    copies back by column name rather than by position."""
    path = str(tmp_path / "older.db")
    _old_case_file(path, oldest_sources=True)
    s = Store(path)
    try:
        s.set_source_nickname(1, "Security log")
        assert s.get_source(1)["nickname"] == "Security log"
        sid = s.ingest_csv(write_csv([["x"], ["new"]], "n.csv"), build_fts=False)["id"]
        assert sid > 9 and s.get_source(sid)["name"] == "n.csv"
    finally:
        s.close()
