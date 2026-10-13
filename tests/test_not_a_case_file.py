"""An existing file that is not a Winnow case is never opened as one.

Store.__init__ runs the case schema against whatever it is handed and
switches the file to WAL, so every way of naming an existing path as a
case is a way of writing into it. /api/case/open has refused a non-case
since it was written; three other doors did not:

- POST /api/cases registered any existing file, which put it in every
  case picker;
- "Copy into this case" (/api/case/copy_sources → Store.copy_sources_to)
  then opened the picked target as a Store — a Chrome History registered
  by mistake gained Winnow's tables, went to WAL and changed hash;
- `server.py --case PATH` opened whatever PATH was.

A path that does not exist yet is a new case and must keep working on
all of them.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys

import pytest

import server
from winnow import workspace as WS
from winnow.store import DEFAULT_TAGS, Store


def _sha(p) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def evidence(tmp_path):
    """A browser history database, as it comes off a collection."""
    d = tmp_path / "evidence"
    d.mkdir()
    p = d / "History"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT)")
    c.execute("INSERT INTO urls (url) VALUES ('https://example.test/')")
    c.commit()
    c.close()
    return p


def _untouched(p, sha: str) -> None:
    assert _sha(p) == sha, "the evidence file was written to"
    assert sorted(x.name for x in p.parent.iterdir()) == [p.name], \
        "a -wal/-shm/lock file appeared next to the evidence"


# ------------------------------------------------------------ registration

def test_registering_an_existing_non_case_file_is_refused(client, evidence):
    sha = _sha(evidence)
    r = client.post("/api/cases", json={"path": str(evidence), "name": "History"})
    assert r.status_code == 400 and "not a Winnow case file" in r.text
    assert WS.cases.find_by_path(str(evidence)) is None
    _untouched(evidence, sha)


def test_registering_a_file_that_is_not_sqlite_at_all_is_refused(client, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("not a database")
    r = client.post("/api/cases", json={"path": str(p), "name": "notes"})
    assert r.status_code == 400
    assert p.read_text() == "not a database"


def test_registering_a_new_path_still_creates_the_case(client, tmp_path):
    p = tmp_path / "new" / "fresh.db-winnow"
    r = client.post("/api/cases", json={"path": str(p), "name": "fresh"})
    assert r.status_code == 200, r.text
    assert server.is_winnow_case_file(str(p))


def test_registering_an_existing_case_still_works(client, tmp_path):
    p = tmp_path / "old.db-winnow"
    Store(str(p), default_tags=DEFAULT_TAGS).close()
    r = client.post("/api/cases", json={"path": str(p), "name": "old"})
    assert r.status_code == 200, r.text


# -------------------------------------------------------- copy into a case

def test_copying_into_a_non_case_file_is_refused(client, store, write_csv, evidence):
    """The registry can already hold such a path — registered before
    registration checked, or a case file since replaced by something else
    — so the copy checks its target itself."""
    sid = store.ingest_csv(write_csv([["a"], ["1"]], "t.csv"), build_fts=False)["id"]
    WS.cases.create(str(evidence), name="History")
    sha = _sha(evidence)
    r = client.post("/api/case/copy_sources",
                    json={"target_path": str(evidence), "source_ids": [sid]})
    assert r.status_code == 400 and "not a Winnow case file" in r.text
    _untouched(evidence, sha)


def test_store_copy_sources_to_refuses_a_non_case_target(store, write_csv, evidence):
    sid = store.ingest_csv(write_csv([["a"], ["1"]], "t.csv"), build_fts=False)["id"]
    sha = _sha(evidence)
    with pytest.raises(ValueError, match="not a Winnow case file"):
        store.copy_sources_to(str(evidence), [sid])
    _untouched(evidence, sha)


def test_copying_into_a_real_case_still_works(store, write_csv, tmp_path):
    sid = store.ingest_csv(write_csv([["a"], ["1"]], "t.csv"), build_fts=False)["id"]
    target = tmp_path / "target.db-winnow"
    Store(str(target), default_tags=DEFAULT_TAGS).close()
    assert store.copy_sources_to(str(target), [sid])["copied"]


# --------------------------------------------------------------------- CLI

class _Served(Exception):
    """Stands in for uvicorn.run: reaching it means main() opened the case."""


@pytest.fixture
def run_main(monkeypatch):
    import uvicorn
    from winnow import instances

    def _serve(*a, **k):
        raise _Served()

    monkeypatch.setattr(uvicorn, "run", _serve)
    monkeypatch.setattr(instances, "register", lambda *a, **k: None)
    monkeypatch.setattr(server, "STORE", None)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["server.py", "--no-browser", *argv])
        try:
            server.main()
        finally:
            if server.STORE is not None and not server.STORE.closed:
                server.STORE.close()

    return run


def test_cli_case_refuses_an_existing_non_case_file(run_main, evidence, capsys):
    sha = _sha(evidence)
    with pytest.raises(SystemExit) as e:
        run_main("--case", str(evidence))
    assert e.value.code == 1
    assert "not a Winnow case file" in capsys.readouterr().err
    _untouched(evidence, sha)


def test_cli_case_still_creates_a_new_case(run_main, tmp_path):
    p = tmp_path / "cli.db-winnow"
    with pytest.raises(_Served):
        run_main("--case", str(p))
    assert server.is_winnow_case_file(str(p))
