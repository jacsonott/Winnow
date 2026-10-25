"""Paths with `#`, `?` and `%` in them — the three characters a SQLite
`file:` URI gives a meaning to.

Every read-only open in Winnow is a URI (`mode=ro` only exists there), and
they used to be built by pasting the path in. On a path with one of these
characters that does not fail, it opens a DIFFERENT file: `#` cuts the
path at a fragment, and `?mode=ro` goes with it, so the truncated name is
opened read-write and created empty. An analyst saw it as a case kept
under `Case #5/` where every read said "no such table: sources", and as a
browser `History#1.db` that listed no tables while an empty `History`
appeared next to it in the evidence folder.

So these assert in those terms — the right tables come back, and nothing
new appears on disk next to the case or the evidence — for each place
that opens a file read-only: the case reader pool, the SQL pane, raw
filter validation, the SQLite and Plaso importers, another case's
watchlist, the multi-case readers and the server's own peeks at case
files. `winnow.paths.sqlite_ro_uri` is the one place the URI is built.
"""

from __future__ import annotations

import ntpath
import os
import re
import sqlite3
from pathlib import Path

import pytest

from winnow import multicase as mc
from winnow import paths, plasoread
from winnow.store import DEFAULT_TAGS, Store

from test_plaso_import import make_old_plaso


def _odd(*names):
    """A `?` name is POSIX-only: Windows file names cannot hold one, so
    nothing an analyst has there can trip over it either."""
    no_qmark = pytest.mark.skipif(os.name == "nt", reason="a Windows file name cannot hold `?`")
    return [pytest.param(n, marks=no_qmark) if "?" in n else n for n in names]


# Each one misparses differently: `#` truncates, `?` truncates and turns
# the rest into query parameters, `%41` silently names a different file
# ("A"), and a bare `%` followed by non-hex is an outright parse error.
ODD = _odd("Case #5", "what?x=1", "a%41b", "100% done")

ROOT = Path(__file__).resolve().parent.parent


def _listing(d) -> list[str]:
    return sorted(os.listdir(d))


def _case(path) -> Store:
    st = Store(str(path), default_tags=DEFAULT_TAGS)
    return st


@pytest.mark.parametrize("folder", ODD)
def test_a_case_in_an_odd_folder_reads_back_what_was_imported(tmp_path, write_csv, folder):
    """The case reader pool, the SQL pane and raw-filter validation all
    open the case file itself read-only."""
    d = tmp_path / folder
    d.mkdir()
    csv_path = write_csv([["a", "b"], ["1", "x"], ["2", "y"]], "in.csv")
    st = _case(d / "case.db-winnow")
    try:
        rec = st.ingest_csv(csv_path, build_fts=False)
        # list_sources reads through the _reader() pool.
        assert [s["name"] for s in st.list_sources()] == ["in.csv"]
        view = st.build_view(rec["id"], {"source_id": rec["id"], "filters": [],
                                          "sort": [{"column": "a", "dir": "desc"}]})
        rows = st.fetch_rows(view["view_id"], 0, 10)["rows"]
        assert [r["cells"][0] for r in rows] == ["2", "1"]
        assert st.run_sql(f"SELECT count(*) AS n FROM src_{rec['id']}")["rows"] == [[2]]
        st.validate_where_fragment(rec["id"], "a = '1'")
    finally:
        st.close()
    # Nothing created beside the folder ("Case ", "what", "a")...
    assert _listing(tmp_path) == sorted([folder, "in.csv"])
    # ...or inside it beyond the case and its own sidecars.
    assert {f for f in _listing(d)} <= {"case.db-winnow", "case.db-winnow-wal",
                                        "case.db-winnow-shm", "case.db-winnow.winnow-lock"}


@pytest.mark.parametrize("name", _odd("History#1.db", "what?.db", "a%41.db", "100% x.db"))
def test_an_odd_sqlite_evidence_name_lists_and_imports_its_own_tables(store, tmp_path, name):
    ev = tmp_path / "evidence"
    ev.mkdir()
    p = ev / name
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE urls (id INTEGER, url TEXT)")
    c.execute("INSERT INTO urls VALUES (1, 'http://example.test/')")
    c.commit()
    c.close()
    # The file `a%41.db` used to be opened as: a different, real database
    # sitting next to it. It must not be the one that answers.
    decoy = ev / "aA.db"
    sqlite3.connect(decoy).execute("CREATE TABLE decoy (x)").connection.close()
    before = _listing(ev)
    evidence_bytes = p.read_bytes()

    preview = store.preview_sqlite_tables(str(p))
    assert [t["name"] for t in preview["tables"]] == ["urls"]
    rec = store.ingest_sqlite_table(str(p), "urls")
    assert rec["row_count"] == 1

    assert _listing(ev) == before            # no stray "History", "what", "a"
    assert p.read_bytes() == evidence_bytes  # and the evidence never written


@pytest.mark.parametrize("folder", _odd("plaso #2", "p?x", "p%41"))
def test_an_odd_plaso_path_is_detected_and_imported(store, tmp_path, folder):
    ev = tmp_path / "evidence"
    d = ev / folder
    d.mkdir(parents=True)
    plaso = d / "t.plaso"
    make_old_plaso(plaso)
    assert plasoread.is_plaso_storage(str(plaso))
    rec = store.ingest_plaso(str(plaso), build_fts=False)
    assert rec["row_count"] > 0
    assert _listing(ev) == [folder] and _listing(d) == ["t.plaso"]


@pytest.fixture
def odd_case(tmp_path, write_csv):
    """A closed case with a watchlist, under a folder whose name breaks a
    pasted-in URI. Built under a plain name and then renamed, so these
    tests pin the readers below rather than Store's own opens (the test
    above has those)."""
    d = tmp_path / "plain"
    d.mkdir()
    st = _case(d / "host.db-winnow")
    st.ingest_csv(write_csv([["Host", "User"], ["H1", "evil.exe"]], "h.csv"),
                  name="h.csv", build_fts=False)
    st.add_indicator("evil.exe", "filename", None, None)
    st.close()
    d = d.rename(tmp_path / "Case #5")
    return str(d / "host.db-winnow")


def test_multicase_reads_and_attaches_a_case_in_an_odd_folder(odd_case, tmp_path):
    with mc.CaseReader(odd_case) as r:
        assert [s["name"] for s in r.sources()] == ["h.csv"]
    out = mc.sweep_values([odd_case], ["evil.exe"])
    assert len(out[0]["hits"]) == 1
    res = mc.query_across([odd_case], "SELECT count(*) AS n FROM c1.sources")
    assert res["rows"] == [[1]]
    assert "Case " not in _listing(tmp_path)


def test_another_cases_watchlist_imports_from_an_odd_folder(store, odd_case, tmp_path):
    assert store.import_watchlist_from_case(odd_case)["added"] == 1
    assert "Case " not in _listing(tmp_path)


def test_the_server_recognises_and_counts_a_case_in_an_odd_folder(client, odd_case, tmp_path):
    import server
    from winnow import workspace as WS

    assert server.is_winnow_case_file(odd_case)
    WS.cases.create(odd_case, name="host")
    listed = [c for c in client.get("/api/cases").json() if c["path"] == odd_case]
    assert listed and listed[0].get("source_count") == 1 and "error" not in listed[0]
    offered = client.get("/api/watchlist/cases").json()
    assert [c["indicator_count"] for c in offered if c["path"] == odd_case] == [1]
    assert "Case " not in _listing(tmp_path)


def test_the_uri_keeps_windows_drive_and_unc_paths_openable():
    """Pinned with ntpath because CI is Linux. A drive path needs the
    leading slash SQLite strips back off on Windows; a UNC path needs the
    four-slash form — `Path.as_uri()`'s `file://server/...` is refused
    outright ("invalid uri authority"), which would stop every case on a
    share from opening."""
    u = paths.sqlite_ro_uri
    assert u(r"C:\Cases\Case #5\x.db-winnow", _path=ntpath) == \
        "file:///C:/Cases/Case%20%235/x.db-winnow?mode=ro"
    assert u(r"\\server\share\100% x?.db", _path=ntpath) == \
        "file:////server/share/100%25%20x%3F.db?mode=ro"
    with pytest.raises(sqlite3.OperationalError, match="authority"):
        sqlite3.connect("file://server/share/x.db?mode=ro", uri=True)


def test_nothing_pastes_a_path_into_a_file_uri():
    """The class, not just the instances: a new `f"file:{path}..."` is
    the bug again."""
    offenders = []
    files = [ROOT / "server.py", *(ROOT / "winnow").rglob("*.py"),
             *(ROOT / "examples" / "plugins").rglob("*.py")]
    for f in files:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""['"]file:\{""", line):
                offenders.append(f"{f.relative_to(ROOT)}:{n}")
    assert offenders == []


def test_the_views_database_works_from_an_odd_tempdir(tmp_path, write_csv, monkeypatch):
    """The views database lives in the platform tempdir off Linux — the
    analyst's profile on Windows, so it carries their user name. The
    writer attaches it by name and every pooled reader attaches it again
    as a URI; both have to survive a quote, a `#` and a `%`."""
    from winnow import store as store_mod

    odd = tmp_path / "O'Brien #1 %41"
    odd.mkdir()
    monkeypatch.setattr(store_mod, "_preferred_views_dir", lambda: str(odd))
    st = _case(tmp_path / "case.db-winnow")
    try:
        rec = st.ingest_csv(write_csv([["a"], ["2"], ["1"]], "in.csv"), build_fts=False)
        view = st.build_view(rec["id"], {"source_id": rec["id"], "filters": [],
                                          "sort": [{"column": "a", "dir": "asc"}]})
        assert view.get("kind") != "root_virtual"     # really in v.view_N
        # fetch_rows reads v.view_N through a pooled reader's own ATTACH.
        assert [r["cells"][0] for r in st.fetch_rows(view["view_id"], 0, 10)["rows"]] == ["1", "2"]
    finally:
        st.close()
    assert _listing(odd) == []                        # and close() still finds it
