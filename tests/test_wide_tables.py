"""Tables with hundreds of columns — flattened JSON gets there easily.

The search target is every column concatenated, `COALESCE(a,'') || ' ' ||
COALESCE(b,'') || …`, and hide-empty-rows ORs a test per column. Written
as one flat chain, SQLite parses either into a left-deep tree one level
per operator, and past SQLITE_MAX_EXPR_DEPTH (1000) the statement is
refused: from about 500 columns every Contains/regex/Advanced search was
a 500, the trigram index build failed silently in its thread, and the
Search-all sweep counted the table as "no matches". Hide empty rows went
the same way at about 1,000.
"""

from __future__ import annotations

import pytest

from winnow import store as store_mod

WIDTHS = [600, 1500]


def _wide(store, n, *, name="wide"):
    """n columns, three rows; row 2's cells are v<col>_2, row 3 is empty."""
    cols = [f"f{i}" for i in range(n)]
    rows = [[f"v{i}_{r}" for i in range(n)] for r in (1, 2)] + [[""] * n]
    return store.ingest_rows(cols, rows, name=name, build_fts=False)["id"]


def _rids(store, spec):
    v = store.build_view(spec["source_id"], spec)
    return [r["rid"] for r in store.fetch_rows(v["view_id"], 0, 10)["rows"]]


@pytest.mark.parametrize("n", WIDTHS)
def test_every_search_mode_scans_a_wide_table(store, n, monkeypatch):
    monkeypatch.setattr(store, "_ensure_fts_building", lambda source_id: None)  # the LIKE path
    sid = _wide(store, n)
    last = f"v{n - 1}_2"
    assert _rids(store, {"source_id": sid, "search": last}) == [2]
    assert _rids(store, {"source_id": sid, "search": f"v{n - 1}_[2]$", "search_mode": "regex"}) == [2]
    assert _rids(store, {"source_id": sid, "search_mode": "advanced",
                         "search_terms": [{"term": "v0_1"}, {"term": last, "connector": "OR"}]}) == [1, 2]


@pytest.mark.parametrize("n", WIDTHS)
def test_the_index_builds_and_holds_the_same_text(store, n):
    sid = _wide(store, n)
    store.build_fts(sid)
    assert store.get_source(sid)["has_fts"]
    doc = store.db.execute(f"SELECT doc FROM src_{sid}_doc WHERE rid=2").fetchone()[0]
    assert doc == " ".join(f"v{i}_2" for i in range(n))
    assert _rids(store, {"source_id": sid, "search": f"v{n - 1}_2"}) == [2]


@pytest.mark.parametrize("n", WIDTHS)
def test_hide_empty_rows_on_a_wide_table(store, n):
    sid = _wide(store, n)
    assert _rids(store, {"source_id": sid, "hide_empty_rows": True}) == [1, 2]


def test_a_narrow_table_s_sql_is_what_it_always_was():
    """The doc view's definition is stored in every case file that has
    an index; a table that never came near the limit keeps the exact
    expression it was built with."""
    assert store_mod._blob_expr(["a", "b", "c"]) == \
        "COALESCE(\"a\",'') || ' ' || COALESCE(\"b\",'') || ' ' || COALESCE(\"c\",'')"


@pytest.mark.parametrize("n", WIDTHS)
def test_search_all_finds_a_match_in_a_wide_table(store, n, monkeypatch):
    monkeypatch.setattr(store, "_ensure_fts_building", lambda source_id: None)
    sid = _wide(store, n)
    hits = store.search_all_sources(query=f"v{n - 1}_2")
    assert [(h["source_id"], h["match_count"]) for h in hits] == [(sid, 1)]


def test_search_all_says_which_table_it_could_not_search(store, write_csv, monkeypatch):
    """A count that raised was recorded as zero, so a table nobody could
    read showed up as "No matches." — the one answer an analyst acts on
    by moving on."""
    monkeypatch.setattr(store, "_ensure_fts_building", lambda source_id: None)
    good = store.ingest_csv(write_csv([["Cmd"], ["mimikatz"]], "good.csv"), build_fts=False)["id"]
    bad = store.ingest_csv(write_csv([["Cmd"], ["mimikatz"]], "bad.csv"), build_fts=False)["id"]
    real = store._search_all_count_sql

    def broken_for_bad(src, *a, **kw):
        sql, params = real(src, *a, **kw)
        return (sql.replace("SELECT 1", "SELECT no_such_column", 1) if src["id"] == bad else sql), params

    monkeypatch.setattr(store, "_search_all_count_sql", broken_for_bad)

    out = store.search_all_sources(query="mimikatz")
    assert [h["source_id"] for h in out if not h.get("error")] == [good]
    failed = [h for h in out if h.get("error")]
    assert [(h["source_id"], h["name"]) for h in failed] == [(bad, "bad.csv")]
    assert "no_such_column" in failed[0]["error"]

    job = store.start_search_all_job(query="mimikatz")
    done = store.wait_for_search_all_job(timeout=30)
    assert done["job_id"] == job["job_id"] and done["error"] is None
    assert [h["source_id"] for h in done["hits"]] == [good]
    assert [(f["source_id"], f["name"]) for f in done["failed"]] == [(bad, "bad.csv")]


def test_a_table_dropped_mid_sweep_is_not_a_failure(store, write_csv, monkeypatch):
    """The one error that does mean "nothing to report": the table is
    gone. It is skipped, as it always was."""
    monkeypatch.setattr(store, "_ensure_fts_building", lambda source_id: None)
    a = store.ingest_csv(write_csv([["Cmd"], ["mimikatz"]], "a.csv"), build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["Cmd"], ["mimikatz"]], "b.csv"), build_fts=False)["id"]
    real = store._search_all_count_sql
    dropped = []

    def drop_b_first(src, *a_, **kw):
        if src["id"] == b and not dropped:
            dropped.append(b)
            store.drop_source(b)
        return real(src, *a_, **kw)

    monkeypatch.setattr(store, "_search_all_count_sql", drop_b_first)
    assert [(h["source_id"], h.get("error")) for h in store.search_all_sources(query="mimikatz")] == [(a, None)]
    assert dropped == [b]
