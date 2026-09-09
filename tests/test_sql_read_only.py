"""The SQL pane's read-only promise, and the two ways it was breakable.

The pane and the cross-case query both refused ATTACH/DETACH/PRAGMA/VACUUM
by scanning the text for those words. The scan blanked quoted literals
first so a keyword inside a value wasn't mistaken for a keyword — but it
had no notion of comments, so a lone quote inside `/* … */` shifted the
parity and blanked the real keyword out of the string being scanned.
`/* ' */ VACUUM INTO 'anywhere.db'` read as harmless and executed.

Two things it reached: `VACUUM INTO` writes a new database at any path
even on a `mode=ro` connection, and `ATTACH` opens a second database that
is writable regardless of the first one's mode. Dashboards store widget
SQL inside the case file and run it on render, so a case file from
somebody else was enough to trigger it.

The text scan is now comment-aware, and — the part that matters — an
authorizer on the connection refuses every action a read-only query
doesn't need. Spelling tricks cannot get past the second one.
"""

from __future__ import annotations

import sqlite3

import pytest

ESCAPES = [
    "/* ' */ VACUUM INTO '{path}'",
    "/* ' */ ATTACH DATABASE '{path}' AS z",
    "-- '\nVACUUM INTO '{path}'",
    "VACUUM INTO '{path}'",
    "ATTACH DATABASE '{path}' AS z",
]


@pytest.mark.parametrize("template", ESCAPES)
def test_no_spelling_of_vacuum_or_attach_writes_a_file(store, write_csv, tmp_path, template):
    store.ingest_csv(write_csv([["a"], ["1"]], "e.csv"), name="e", build_fts=False)
    target = tmp_path / "escaped.db"
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        store.run_sql(template.format(path=target))
    assert not target.exists(), f"{template!r} wrote a file outside the case"


def test_the_pane_still_runs_ordinary_queries(store, write_csv):
    sid = store.ingest_csv(write_csv([["a"], ["1"], ["2"]], "e.csv"), name="e", build_fts=False)["id"]
    res = store.run_sql(f'SELECT "a" FROM src_{sid} ORDER BY "a"')
    assert [r[0] for r in res["rows"]] == ["1", "2"]


def test_comments_and_quoted_keywords_are_still_allowed():
    """The scan must not overreach: a comment is legal SQL, and a value
    that happens to read like a keyword is ordinary forensic data."""
    from winnow.store import _blank_string_literals, _strip_sql_comments

    for ok in ("SELECT * FROM src_1 -- a note",
               "SELECT 'vacuum the office' FROM src_1",
               "/* explanation */ SELECT * FROM src_1"):
        from winnow.store import Store
        assert not Store.SQL_PANE_FORBIDDEN_RE.search(
            _blank_string_literals(_strip_sql_comments(ok))), ok


def test_sql_to_table_refuses_the_same_escapes(store, write_csv, tmp_path):
    store.ingest_csv(write_csv([["a"], ["1"]], "e.csv"), name="e", build_fts=False)
    target = tmp_path / "escaped2.db"
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        store.sql_to_table(f"/* ' */ VACUUM INTO '{target}'", "t")
    assert not target.exists()


def test_the_authorizer_refuses_writes_the_text_scan_never_looked_for(store, write_csv):
    """The scan only ever knew four words. The authorizer is what makes
    'read-only' true for the ones nobody thought to list."""
    sid = store.ingest_csv(write_csv([["a"], ["1"]], "e.csv"), name="e", build_fts=False)["id"]
    ro = store._pane_connection()
    try:
        for sql in (f'DELETE FROM src_{sid}',
                    f'UPDATE src_{sid} SET "a" = 2',
                    'CREATE TABLE evil(x)',
                    'CREATE TEMP TABLE evil2(x)'):
            with pytest.raises(sqlite3.DatabaseError):
                ro.execute(sql)
    finally:
        ro.close()


# ------------------------------------------------- cross-case identifiers

def test_a_quote_in_a_header_does_not_silently_drop_a_source_from_a_sweep(tmp_path):
    """Column names are user data (invariant #5). The cross-case sweep built
    them into SQL by hand, and `sanitize_columns` does not strip a double
    quote — so a header like `Path"Name` produced a syntax error that
    `except sqlite3.Error: continue` swallowed. The source vanished from the
    sweep and an indicator that IS present reported zero hits: a false
    negative in the one feature whose whole job is "is this anywhere".
    """
    import csv

    from winnow import multicase as mc
    from winnow.store import Store

    case = tmp_path / "c.db-winnow"
    src = tmp_path / "e.csv"
    with open(src, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(['Path"Name', "Other"])
        w.writerow(["C:/evil.exe", "x"])
    store = Store(str(case))
    try:
        store.ingest_csv(str(src), name="e", build_fts=False)
    finally:
        store.close()

    (result,) = mc.sweep_values([str(case)], ["evil.exe"])
    assert result["error"] is None, result["error"]
    assert result["hits"], "the source with a quoted header was skipped from the sweep"
    assert result["hits"][0]["rid"] == 1
