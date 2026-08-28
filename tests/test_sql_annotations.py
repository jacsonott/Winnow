"""Tags and Note as queryable columns in the SQL pane's src_N / merge_N
views."""

from __future__ import annotations


def _mk(store, write_csv):
    sid = store.ingest_csv(write_csv([["Msg"], ["alpha"], ["beta"], ["gamma"]], "a.csv"),
                           name="a", build_fts=False)["id"]
    tags = store.list_tags()
    store.set_tags(sid, [1, 3], tags[0]["id"], True)
    store.set_tags(sid, [1], tags[1]["id"], True)
    store.set_note(sid, 2, "check this")
    return sid, tags


def test_tags_and_note_are_columns(store, write_csv):
    sid, tags = _mk(store, write_csv)
    out = store.run_sql(f'SELECT rid, Tags, Note FROM src_{sid} ORDER BY rid')
    rows = {r[0]: (r[1], r[2]) for r in out["rows"]}
    assert tags[0]["name"] in rows[1][0] and tags[1]["name"] in rows[1][0]
    assert rows[2] == (None, "check this")
    assert rows[3][0] == tags[0]["name"]


def test_tags_filter_and_group_like_any_column(store, write_csv):
    sid, tags = _mk(store, write_csv)
    out = store.run_sql(
        f"SELECT COUNT(*) FROM src_{sid} WHERE Tags LIKE '%{tags[0]['name']}%'")
    assert out["rows"][0][0] == 2
    out = store.run_sql(f"SELECT Msg FROM src_{sid} WHERE Note IS NOT NULL")
    assert out["rows"] == [["beta"]]


def test_merge_views_carry_tags_and_note(store, write_csv):
    a, tags = _mk(store, write_csv)
    b = store.ingest_csv(write_csv([["Msg"], ["delta"]], "b.csv"), name="b", build_fts=False)["id"]
    mid = store.create_merge("m", [a, b])["id"]
    out = store.run_sql(f"SELECT source_id, rid, Tags FROM merge_{-mid} WHERE Tags IS NOT NULL ORDER BY rid")
    assert [(r[0], r[1]) for r in out["rows"]] == [(a, 1), (a, 3)]


def test_a_real_tags_column_wins_the_plain_name(store, write_csv):
    """A CSV that genuinely has a Tags column keeps it — Winnow's own
    annotation steps aside to 'Winnow Tags'."""
    sid = store.ingest_csv(write_csv([["Tags", "X"], ["from-file", "1"]], "t.csv"),
                           name="t", build_fts=False)["id"]
    store.set_tags(sid, [1], store.list_tags()[0]["id"], True)
    out = store.run_sql(f'SELECT Tags, "Winnow Tags" FROM src_{sid}')
    assert out["rows"][0][0] == "from-file"
    assert out["rows"][0][1] == store.list_tags()[0]["name"]
