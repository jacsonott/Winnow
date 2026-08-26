"""The __line__ pseudo-column: original file order as an explicit,
reversible sort — the way back from any other sort."""

from __future__ import annotations


ROWS = [["Name", "N"]] + [[f"row{i}", str((i * 7) % 10)] for i in range(1, 11)]


def _lines(store, sid, spec_sort):
    v = store.build_view(sid, {"source_id": sid, "filters": [], "sort": spec_sort})
    rows = store.fetch_rows(v["view_id"], 0, 20)["rows"]
    return [r["cells"][0] for r in rows]


def test_line_desc_reverses_the_file_order(store, write_csv):
    rec = store.ingest_csv(write_csv(ROWS, "l.csv"), name="l", build_fts=False)
    asc = _lines(store, rec["id"], [])
    desc = _lines(store, rec["id"], [{"column": "__line__", "dir": "desc"}])
    assert asc == [f"row{i}" for i in range(1, 11)]
    assert desc == list(reversed(asc))
    # explicit ascending is the same order the empty sort gives
    assert _lines(store, rec["id"], [{"column": "__line__", "dir": "asc"}]) == asc


def test_line_sort_never_collides_with_a_real_column(store, write_csv):
    """A CSV could carry a column literally named __line__ — the pseudo-
    column wins deliberately (it's not reachable from the header UI), and
    nothing crashes."""
    rec = store.ingest_csv(write_csv([["__line__"], ["b"], ["a"]], "c.csv"),
                           name="c", build_fts=False)
    v = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [],
                                     "sort": [{"column": "__line__", "dir": "desc"}]})
    rows = store.fetch_rows(v["view_id"], 0, 5)["rows"]
    assert [r["cells"][0] for r in rows] == ["a", "b"]  # reverse file order, not alphabetical
