"""store.py run_sql: the ATTACH/DETACH/PRAGMA/VACUUM blacklist, that it
doesn't false-positive on identifiers merely containing those words, and
limit/truncated reporting."""

from __future__ import annotations

import pytest


def test_run_sql_allows_select(ingested):
    store, source_id = ingested
    res = store.run_sql(f"SELECT EventId FROM src_{source_id} ORDER BY rid")
    assert res["columns"] == ["EventId"]
    assert [r[0] for r in res["rows"]] == ["4624", "4688", "4625", "1"]
    assert res["truncated"] is False


def test_run_sql_allows_explain(ingested):
    store, source_id = ingested
    res = store.run_sql(f"EXPLAIN QUERY PLAN SELECT * FROM src_{source_id}")
    assert res["columns"]


@pytest.mark.parametrize("sql", [
    "ATTACH DATABASE 'x.db' AS x",
    "DETACH DATABASE x",
    "PRAGMA table_info(sources)",
    "VACUUM",
])
def test_run_sql_blocks_forbidden_statements(store, sql):
    with pytest.raises(ValueError):
        store.run_sql(sql)


def test_run_sql_does_not_false_positive_on_identifier_substrings(store, write_csv):
    # Word-boundary matched, not a bare substring search — a column named
    # "Attachment" (a real, plausible email-export header) must not trip
    # the ATTACH blacklist.
    path = write_csv([["Attachment"], ["invoice.pdf"]])
    rec = store.ingest_csv(path, name="mail.csv", build_fts=False)
    res = store.run_sql(f"SELECT Attachment FROM src_{rec['id']}")
    assert res["rows"] == [["invoice.pdf"]]


def test_run_sql_limit_and_truncated(store, tmp_path):
    path = tmp_path / "many.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("N\n")
        for i in range(20):
            f.write(f"{i}\n")
    rec = store.ingest_csv(str(path), name="many.csv", build_fts=False)
    res = store.run_sql(f"SELECT N FROM src_{rec['id']} ORDER BY rid", limit=5)
    assert len(res["rows"]) == 5
    assert res["truncated"] is True

    res_full = store.run_sql(f"SELECT N FROM src_{rec['id']} ORDER BY rid", limit=100)
    assert len(res_full["rows"]) == 20
    assert res_full["truncated"] is False
