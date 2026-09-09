"""The Host gate, and why the client-header gate needs it.

Every state-changing route is guarded by a custom request header, on the
reasoning that a cross-origin page cannot set one without a preflight.
That holds until the page IS same-origin — which is exactly what DNS
rebinding arranges: a page served from evil.com re-resolves its own name
to 127.0.0.1, and the browser then treats it as same-origin with Winnow.
It can set any header it likes and read the responses, and the TCP peer
genuinely is 127.0.0.1, so the loopback checks pass too.

The Host header is what tells the two apart, because the browser sends
the name it resolved rather than the address it reached.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture
def raw(store, monkeypatch):
    monkeypatch.setattr(server, "STORE", store)
    return TestClient(server.app, headers={"X-Timeline-Lite-Client": "1"})


@pytest.mark.parametrize("host", ["localhost", "localhost:8777", "127.0.0.1:8777",
                                  "[::1]:8777", "192.168.1.10:8777"])
def test_addresses_winnow_told_you_about_are_accepted(raw, host):
    """Loopback names, and any IP literal — an address has no name to
    re-resolve, so it cannot be the far end of a rebind."""
    r = raw.get("/api/sources", headers={"Host": host})
    assert r.status_code == 200, (host, r.status_code)


@pytest.mark.parametrize("host", ["evil.com", "evil.com:8777", "winnow.attacker.test",
                                  "rebind.localhost.evil.com"])
def test_a_name_nobody_configured_is_refused(raw, host):
    r = raw.get("/api/sources", headers={"Host": host})
    assert r.status_code == 421, (host, r.status_code)


def test_the_refusal_covers_writes_too(raw):
    r = raw.post("/api/tags", json={"name": "X", "color": "#000000"},
                 headers={"Host": "evil.com"})
    assert r.status_code == 421


def test_an_operator_can_name_their_own_front_end(raw, monkeypatch):
    """--allow-host, for a reverse proxy or a hostname on a lab network."""
    monkeypatch.setattr(server, "ALLOWED_HOSTS", server.ALLOWED_HOSTS | {"winnow.lab"})
    assert raw.get("/api/sources", headers={"Host": "winnow.lab"}).status_code == 200
    assert raw.get("/api/sources", headers={"Host": "other.lab"}).status_code == 421


def test_a_missing_host_is_refused():
    """HTTP/1.1 requires it, so absent means hand-crafted."""
    assert server._host_is_allowed("") is False
    assert server._host_is_allowed(None) is False


# ------------------------------------------------- the rest of the write surface

def test_opening_a_non_case_sqlite_file_is_refused(client, tmp_path):
    """Store.__init__ runs META_SCHEMA against whatever it is handed, so
    this route would add Winnow's tables to a browser history database, or
    to a mounted piece of evidence, in place. is_winnow_case_file has
    existed for the double-click handler since before the route did."""
    import sqlite3

    other = tmp_path / "places.sqlite"
    sqlite3.connect(other).execute("CREATE TABLE moz_places(id INTEGER)")
    before = sorted(p.name for p in tmp_path.iterdir())

    r = client.post("/api/case/open", json={"path": str(other)})
    assert r.status_code == 400 and "not a Winnow case file" in r.text
    with sqlite3.connect(f"file:{other}?mode=ro", uri=True) as c:
        names = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == {"moz_places"}, "the route wrote its schema into someone else's database"
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_deleting_a_case_file_requires_it_to_be_one(client, tmp_path):
    """Registering a case only records a path; nothing checked what was at
    the end of it, so `delete_file=true` was an unlink of anything the
    analyst's account could reach."""
    victim = tmp_path / "id_ed25519"
    victim.write_text("PRIVATE KEY")
    rec = client.post("/api/cases", json={"path": str(victim), "name": "not a case"})
    assert rec.status_code in (200, 400)
    if rec.status_code != 200:
        pytest.skip("registration refuses this path outright, which is also fine")
    cid = rec.json()["id"]
    r = client.delete(f"/api/cases/{cid}?delete_file=true")
    assert r.status_code == 400 and "not a Winnow case file" in r.text
    assert victim.read_text() == "PRIVATE KEY"


def test_a_plugin_cannot_write_into_the_evidence(store, write_csv):
    """Invariant #1, and invariant #2's paging carve-out with it: `pos =
    rid - 1` is exact only while no source table is ever mutated."""
    sid = store.ingest_csv(write_csv([["a"], ["1"], ["2"]], "e.csv"), name="e", build_fts=False)["id"]
    store.plugin_table_create("demo", "notes", "id INTEGER PRIMARY KEY, body TEXT")
    for sql in (f'DELETE FROM src_{sid}',
                f'UPDATE src_{sid} SET "a" = 9',
                "DELETE FROM row_tags",
                f'/* sneaky */ DELETE FROM src_{sid}'):
        with pytest.raises(ValueError, match="may not write"):
            store.plugin_table_write("demo", "notes", sql)
    assert store.fetch_rows(store.build_view(sid, {})["view_id"], 0, 10)["rows"]
    # Its own table is still perfectly writable.
    store.plugin_table_write("demo", "notes", "INSERT INTO {table}(body) VALUES ('ok')")
    assert len(store.plugin_table_rows("demo", "notes")) == 1
