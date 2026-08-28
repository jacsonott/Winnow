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


def test_spec_sql_renders_the_live_filter_as_runnable_sql(ingested):
    """spec_sql is 'open this filter in the SQL pane': the compiled text
    must (a) run unchanged on run_sql's own connection and (b) return the
    same rows the view shows — it's the same _compile_where, with params
    inlined. A value with a quote in it and a raw fragment containing a
    literal ? inside a string are the two inlining traps."""
    store, sid = ingested
    spec = {
        "filters": [{"column": "User", "op": "contains", "value": "ACME"}],
        "sort": [{"column": "Timestamp", "dir": "desc"}],
        "filter_tree": {"type": "group", "op": "AND", "children": [
            {"type": "raw", "sql": "\"CommandLine\" <> 'what?'"},
        ]},
        "time_range": {"enabled": True, "start": "2024-01-05 00:00:00", "end": "2024-01-06 23:59:59"},
    }
    sql = store.spec_sql(sid, spec)
    res = store.run_sql(sql)
    view = store.build_view(sid, spec)
    view_rids = [r["rid"] for r in store.fetch_rows(view["view_id"], 0, 10)["rows"]]
    rid_col = res["columns"].index("rid")
    assert [r[rid_col] for r in res["rows"]] == view_rids
    # The literal ? inside the raw fragment's string must survive inlining
    # untouched — a naive placeholder substitution would corrupt it.
    assert "'what?'" in sql


def test_spec_sql_inlines_quotes_safely(ingested):
    store, sid = ingested
    sql = store.spec_sql(sid, {"filters": [{"column": "User", "op": "equals", "value": "o'malley"}]})
    assert "'o''malley'" in sql
    assert store.run_sql(sql)["rows"] == []  # runs clean, matches nothing


def test_sql_pane_connection_has_the_timestamp_functions(ingested):
    """spec_sql output can contain TS_NORMALIZE/DAY_BUCKET (the timeframe
    filter compiles to them), so run_sql's connection must register the
    same trio the writer and reader pool do."""
    store, sid = ingested
    res = store.run_sql(f"SELECT DAY_BUCKET(\"Timestamp\") AS d FROM src_{sid} ORDER BY rid LIMIT 1")
    assert res["rows"][0][0] == "2024-01-05"


def test_sql_to_table_lands_a_real_source(store, write_csv):
    sid = store.ingest_csv(write_csv([["A"], ["x"], ["y"], ["z"]], "t.csv"),
                           name="t", build_fts=False)["id"]
    res = store.sql_to_table(f"SELECT A, A || '!' AS Loud FROM src_{sid} WHERE A != 'y'", "kept")
    src = store.get_source(res["source"]["id"])
    assert src["row_count"] == 2
    assert [c["name"] for c in src["columns"]] == ["A", "Loud"]


def test_sql_to_table_soft_cap_asks_then_obeys_force(store, write_csv, monkeypatch):
    sid = store.ingest_csv(write_csv([["A"]] + [[str(i)] for i in range(50)], "c.csv"),
                           name="c", build_fts=False)["id"]
    monkeypatch.setattr(type(store), "SQL_TO_TABLE_SOFT_CAP", 10)
    res = store.sql_to_table(f"SELECT A FROM src_{sid}", "big")
    assert res == {"needs_confirm": True, "rows": 50}
    res = store.sql_to_table(f"SELECT A FROM src_{sid}", "big", force=True)
    assert res["source"]["row_count"] == 50


def test_sql_to_table_rejects_writes_and_empty(store, write_csv):
    import pytest as _pytest

    store.ingest_csv(write_csv([["A"], ["x"]], "r.csv"), name="r", build_fts=False)
    with _pytest.raises(ValueError):
        store.sql_to_table("PRAGMA page_size", "nope")
    with _pytest.raises(ValueError):
        store.sql_to_table("SELECT 1", "  ")
