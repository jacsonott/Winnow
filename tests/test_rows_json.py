"""Store.fetch_rows_json — a grid page with its cells written by SQLite —
against fetch_rows plus json.dumps as JSONResponse writes it, which is
what GET /api/rows sent before. Byte for byte: the grid parses the page,
so a cell that came back with fewer digits, a different escape or a
column off by one would show the analyst a value the evidence does not
hold."""

from __future__ import annotations

import json
import sqlite3

import pytest

from winnow.store import JSON_CELLS_PER_CALL


def _old(store, view_id, start=0, count=5000):
    return json.dumps(store.fetch_rows(view_id, start, count), ensure_ascii=False, allow_nan=False,
                      indent=None, separators=(",", ":")).encode("utf-8")


def _same(store, view_id, start=0, count=5000):
    got = store.fetch_rows_json(view_id, start, count)
    assert got == _old(store, view_id, start, count)
    return json.loads(got)


# Every control character, both kinds of quote, a backslash, a slash, the
# line and paragraph separators, astral and combining characters, and
# values that look like JSON or SQL without being either.
AWKWARD = ["".join(chr(c) for c in range(1, 32)), 'a "quoted" \\ back/slash', "  ",
           "\U0001F600 é é 中文", "[1,2]", '{"k": null}', "null", "-0", "1e5", "",
           "   leading", "trailing   ", "x" * 5000, "O'Brien; DROP TABLE x"]


@pytest.fixture
def awkward(store, write_csv):
    rows = [["When", "Text", "Host"]]
    for i, text in enumerate(AWKWARD * 3):
        rows.append([f"2026-03-14 {i % 24:02d}:{i % 60:02d}:00", text, f"H{i % 4}"])
    return store.ingest_csv(write_csv(rows, "awkward.csv"), name="awkward", build_fts=False)["id"]


def test_every_kind_of_text_comes_out_as_json_dumps_writes_it(store, awkward):
    view = store.build_view(awkward, {"source_id": awkward})
    assert store._views[view["view_id"]]["kind"] == "root_virtual"
    page = _same(store, view["view_id"])
    assert [r["cells"][1] for r in page["rows"][:len(AWKWARD)]] == AWKWARD


@pytest.mark.parametrize("spec", [
    {"sort": [{"column": "Text", "dir": "desc"}]},
    {"filters": [{"column": "Host", "op": "equals", "value": "H1"}]},
    {"filters": [{"column": "Host", "op": "equals", "value": "nobody"}]},
], ids=["sorted", "filtered", "empty"])
def test_a_materialised_view(store, awkward, spec):
    view = store.build_view(awkward, {"source_id": awkward, **spec})
    _same(store, view["view_id"])
    _same(store, view["view_id"], start=7, count=5)


def test_a_window_past_the_end_and_one_partly_past_it(store, awkward):
    view = store.build_view(awkward, {"source_id": awkward})
    assert _same(store, view["view_id"], start=10_000, count=50)["rows"] == []
    assert len(_same(store, view["view_id"], start=40, count=50)["rows"]) == 3 * len(AWKWARD) - 40


def test_tags_and_notes_ride_along(store, awkward):
    red = store.upsert_tag(None, "Red", "#ff0000", "1")["id"]
    blue = store.upsert_tag(None, "Blue", "#0000ff", "2")["id"]
    store.set_tags(awkward, [1, 2, 5], blue, True)
    store.set_tags(awkward, [2, 3], red, True)
    store.set_note(awkward, 2, 'a "note"\nwith \\ lines   and \U0001F600')
    store.set_note(awkward, 4, "")
    for spec in ({}, {"sort": [{"column": "Host", "dir": "asc"}]}):
        view = store.build_view(awkward, {"source_id": awkward, **spec})
        rows = {r["rid"]: r for r in _same(store, view["view_id"])["rows"]}
        assert rows[2]["tags"] == sorted([red, blue]) and rows[5]["tags"] == [blue]
        assert rows[2]["note"].startswith('a "note"') and rows[4]["note"] is None


def test_wider_than_one_json_array_call(store, write_csv):
    """More columns than json_array takes at once (SQLite's 127-argument
    cap): the cells go in calls of JSON_CELLS_PER_CALL and are joined."""
    n = JSON_CELLS_PER_CALL * 2 + 37
    cols = [f"c{i}" for i in range(n)]
    rows = [cols] + [[f"r{r}c{i}" if i % 3 else "" for i in range(n)] for r in range(20)]
    sid = store.ingest_csv(write_csv(rows, "wide.csv"), name="wide", build_fts=False)["id"]
    for spec in ({}, {"sort": [{"column": "c1", "dir": "desc"}]}):
        view = store.build_view(sid, {"source_id": sid, **spec})
        page = _same(store, view["view_id"])
        assert len(page["rows"][0]["cells"]) == n


def test_exactly_one_call_wide(store, write_csv):
    cols = [f"c{i}" for i in range(JSON_CELLS_PER_CALL)]
    sid = store.ingest_csv(write_csv([cols, cols], "w.csv"), name="w", build_fts=False)["id"]
    _same(store, store.build_view(sid, {"source_id": sid})["view_id"])


def test_a_merge_of_tables_with_different_columns(store, awkward, write_csv):
    other = store.ingest_csv(write_csv([["HOST", "Extra", "When"], ["Z1", "only here", "2026-03-15 00:00:00"],
                                        ["Z2", "\x01", "2026-03-15 01:00:00"]], "other.csv"),
                             name="other", build_fts=False)["id"]
    mid = store.create_merge("both", [awkward, other])["id"]
    store.set_tags(other, [2], store.upsert_tag(None, "Red", "#ff0000", "1")["id"], True)
    for spec in ({}, {"sort": [{"column": "When", "dir": "desc"}]},
                 {"filters": [{"column": "Host", "op": "equals", "value": "Z2"}]}):
        view = store.build_view(mid, {"source_id": mid, **spec})
        page = _same(store, view["view_id"])
        assert {r["source_id"] for r in page["rows"]} >= ({other} if spec.get("filters") else {awkward, other})


def test_derived_columns_including_empty_ones(store, awkward):
    res = store.add_derived_column(awkward, "HostNo", "Host", "regex_extract", {"pattern": r"H([0-2])"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    res = store.add_derived_column(awkward, "WhenISO", "When", "iso8601", {})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    for spec in ({}, {"sort": [{"column": "HostNo", "dir": "asc"}]}):
        view = store.build_view(awkward, {"source_id": awkward, **spec})
        page = _same(store, view["view_id"])
        assert any(r["cells"][3] in (None, "") for r in page["rows"])   # H3 extracts nothing


def test_groups_both_ways(store, awkward):
    """A small group of an unfiltered table pages the table itself
    (group_virtual); a group of a filtered view gets a sub-view."""
    root = store.build_view(awkward, {"source_id": awkward})["view_id"]
    small = store.expand_group(root, "Host", "H1")
    assert store._views[small["view_id"]]["kind"] == "group_virtual"
    _same(store, small["view_id"])
    _same(store, small["view_id"], start=3, count=4)
    filtered = store.build_view(awkward, {"source_id": awkward,
                                          "filters": [{"column": "Host", "op": "not_equals", "value": "H0"}]})
    sub = store.expand_group(filtered["view_id"], "Host", "H2")
    assert store._views[sub["view_id"]]["kind"] == "group"
    _same(store, sub["view_id"])


def test_a_number_put_in_a_cell_is_stored_as_text(store, write_csv):
    """Why SQLite's JSON is trusted with the cells: the one value it writes
    differently from json.dumps is a REAL, and a cell can never hold one.
    Every column a page reads is declared TEXT, so even a number written
    straight into one is stored as its text, and the page carries that
    text as it is stored.

    The expected cells are read back from the table rather than spelled
    out: how many digits SQLite gives a REAL it converts to TEXT depends
    on its version (15 significant digits, "0.3", on the SQLite CI's
    Pythons use; 17, "0.30000000000000004", on SQLite 3.53), and either
    way the page has to show what the table holds."""
    sid = store.ingest_csv(write_csv([["A", "B"], ["x", "y"]], "n.csv"), name="n", build_fts=False)["id"]
    table = store.get_source(sid)["table_name"]
    res = store.add_derived_column(sid, "D", "A", "regex_extract", {"pattern": "(x)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    with store.lock, store.db:
        store.db.execute(f'UPDATE "{table}" SET "A" = ?, "B" = ? WHERE rid = 1', (0.1 + 0.2, 7))
        store.db.execute(f'UPDATE "drv_{sid}" SET "D" = ? WHERE rid = 1', (1e20,))
        assert tuple(store.db.execute(f'SELECT typeof("A"), typeof("B") FROM "{table}"').fetchone()) == ("text", "text")
        stored = [*store.db.execute(f'SELECT "A", "B" FROM "{table}" WHERE rid = 1').fetchone(),
                  store.db.execute(f'SELECT "D" FROM "drv_{sid}" WHERE rid = 1').fetchone()[0]]
    assert all(isinstance(v, str) for v in stored), stored
    assert stored[0] in ("0.3", "0.30000000000000004") and stored[1:] == ["7", "1.0e+20"], stored
    page = _same(store, store.build_view(sid, {"source_id": sid})["view_id"])
    assert page["rows"][0]["cells"] == stored


def test_a_blob_fails_as_it_always_did(store, write_csv):
    """SQLite's JSON refuses a BLOB; the page is then written the old way,
    which refuses it too, the same way."""
    sid = store.ingest_csv(write_csv([["A"]] + [["x"]] * 5, "b.csv"), name="b", build_fts=False)["id"]
    table = store.get_source(sid)["table_name"]
    with store.lock, store.db:
        store.db.execute(f'UPDATE "{table}" SET "A" = ? WHERE rid = 3', (b"\x00\xff",))
    view = store.build_view(sid, {"source_id": sid})["view_id"]
    with pytest.raises(TypeError):
        _old(store, view)
    with pytest.raises(TypeError):
        store.fetch_rows_json(view, 0, 100)
    assert store.fetch_rows_json(view, 3, 2) == _old(store, view, 3, 2)   # the rows after it are fine


def test_a_sqlite_that_cannot_takes_the_old_path(store, awkward, monkeypatch):
    """A build whose argument cap is below JSON_CELLS_PER_CALL fails the
    probe, and every page is written by json.dumps."""
    if not hasattr(sqlite3.Connection, "setlimit"):
        pytest.skip("Connection.setlimit needs Python 3.11+")
    real = store._open_reader

    def capped():
        ro = real()
        ro.setlimit(sqlite3.SQLITE_LIMIT_FUNCTION_ARG, JSON_CELLS_PER_CALL - 1)
        return ro

    monkeypatch.setattr(store, "_open_reader", capped)
    with store._reader_lock:
        idle, store._reader_pool = store._reader_pool, []
    for conn in idle:
        conn.close()
    store._sql_json_ok = None
    view = store.build_view(awkward, {"source_id": awkward})["view_id"]
    _same(store, view)
    assert store._sql_json_ok is False


def test_an_expired_view_is_still_a_409(client, store, awkward):
    view = store.build_view(awkward, {"source_id": awkward})["view_id"]
    store.build_view(awkward, {"source_id": awkward, "sort": [{"column": "Host", "dir": "asc"}]})
    assert client.get("/api/rows", params={"view_id": view, "start": 0, "count": 10}).status_code == 409


def test_the_route_sends_the_encoded_page(client, store, awkward):
    view = store.build_view(awkward, {"source_id": awkward, "sort": [{"column": "Host", "dir": "asc"}]})["view_id"]
    r = client.get("/api/rows", params={"view_id": view, "start": 2, "count": 9})
    assert r.status_code == 200 and r.headers["content-type"] == "application/json"
    assert r.content == _old(store, view, 2, 9)
