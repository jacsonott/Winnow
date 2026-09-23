"""The tag filter is a union of whatever it names.

The ribbon could only ever send one thing: one tag, or "any tag", or
nothing. The compile matched the whole list against `["__any__"]` and
`["__none__"]` as exact values, so a list holding a sentinel alongside
real ids fell through to the ids branch and dropped the sentinel without
saying so. Each part compiles on its own now and the parts are OR'd —
which is what makes "either of these two tags" and "untagged, plus the
ones I marked" expressible at all.

OR and not AND: two ticked chips mean rows carrying either, the way two
ticked values in the value picker do. An intersection belongs in the
filter builder, which can say it.
"""

from __future__ import annotations

import pytest

from winnow.store import Store


@pytest.fixture
def case(tmp_path):
    p = tmp_path / "rows.csv"
    p.write_text("Host,EventId\n" + "".join(f"H{i},{4624 + i}\n" for i in range(10)), encoding="utf-8")
    store = Store(str(tmp_path / "case.db"))
    try:
        src = store.ingest_csv(str(p))
        red = store.upsert_tag(None, "Red", "#ff0000", "1")
        blue = store.upsert_tag(None, "Blue", "#0000ff", "2")
        # rids 1,2 red; rid 3 blue; 4..10 untagged
        store.set_tags(src["id"], [1, 2], red["id"], True)
        store.set_tags(src["id"], [3], blue["id"], True)
        yield store, src, red, blue
    finally:
        store.close()


def _rows(store, src, tags):
    v = store.build_view(src["id"], {"filters": [], "sort": [], "tags": tags})
    return v["row_count"]


def test_two_tags_means_either_of_them(case):
    store, src, red, blue = case
    assert _rows(store, src, [red["id"]]) == 2
    assert _rows(store, src, [blue["id"]]) == 1
    assert _rows(store, src, [red["id"], blue["id"]]) == 3


def test_no_tags_is_the_rows_nothing_is_on(case):
    store, src, red, blue = case
    assert _rows(store, src, ["__none__"]) == 7


def test_no_tags_beside_a_tag_is_the_union_of_the_two(case):
    """The case the old exact-match compile dropped on the floor: it saw a
    list that was not exactly ["__none__"], took the ids branch, and
    answered "rows tagged Red" for a filter that asked for more."""
    store, src, red, blue = case
    assert _rows(store, src, ["__none__", red["id"]]) == 9
    assert _rows(store, src, ["__none__", red["id"], blue["id"]]) == 10


def test_any_tag_still_means_every_tagged_row(case):
    store, src, red, blue = case
    assert _rows(store, src, ["__any__"]) == 3
    # Any tag already contains Red, so the union is unchanged by it — the
    # ribbon drops "Any tag" when a specific one is picked for that reason,
    # but a spec that sends both must not mean something else.
    assert _rows(store, src, ["__any__", red["id"]]) == 3
    assert _rows(store, src, ["__any__", "__none__"]) == 10


def test_an_empty_tag_filter_is_no_filter(case):
    store, src, red, blue = case
    assert _rows(store, src, []) == 10


def test_it_works_the_same_on_a_merge(tmp_path):
    """Invariant #9: a merge compiles per member, so the union has to be
    per member too — a row is in scope if its OWN table's tags say so."""
    store = Store(str(tmp_path / "case.db"))
    try:
        paths = []
        for n in ("a.csv", "b.csv"):
            p = tmp_path / n
            p.write_text("Host,EventId\n" + "".join(f"H{i},{4624 + i}\n" for i in range(4)), encoding="utf-8")
            paths.append(p)
        a = store.ingest_csv(str(paths[0]))
        b = store.ingest_csv(str(paths[1]))
        red = store.upsert_tag(None, "Red", "#ff0000", "1")
        blue = store.upsert_tag(None, "Blue", "#0000ff", "2")
        store.set_tags(a["id"], [1], red["id"], True)
        store.set_tags(b["id"], [1], blue["id"], True)
        merge = store.create_merge("both", [a["id"], b["id"]])

        def rows(tags):
            v = store.build_view(merge["id"], {"filters": [], "sort": [], "tags": tags})
            return v["row_count"]

        assert rows([red["id"]]) == 1
        assert rows([red["id"], blue["id"]]) == 2
        assert rows(["__none__"]) == 6
        assert rows(["__none__", red["id"]]) == 7
    finally:
        store.close()
