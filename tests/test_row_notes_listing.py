"""The Notes page lists the case's ROW notes, not only the narrative.

Two different things were called notes: the case narrative on the Notes
page, and the notes an analyst writes on evidence rows in the detail pane.
The second kind existed only where it was written, so writing up findings
meant remembering which rows had been annotated. `Store.list_row_notes` is
what the page asks; these pin what it has to answer for it to be any use —
the table's own name on every entry, an honest total when there are more
notes than the listing shows, and nothing left over from a table that has
since been removed from the case.
"""

from __future__ import annotations

A_ROWS = [["When", "Msg"],
          ["2024-01-01 10:00", "alpha"],
          ["2024-01-01 11:00", "beta"],
          ["2024-01-01 12:00", "gamma"]]
B_ROWS = [["When", "Msg"],
          ["2024-01-02 10:00", "delta"],
          ["2024-01-02 11:00", "epsilon"]]


def _two_tables(store, write_csv):
    a = store.ingest_csv(write_csv(A_ROWS, "a.csv"), name="security.csv", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(B_ROWS, "b.csv"), name="mft.csv", build_fts=False)["id"]
    return a, b


def test_every_note_says_which_table_and_line_it_is_on(store, write_csv):
    a, b = _two_tables(store, write_csv)
    store.set_note(b, 2, "staged here")
    store.set_note(a, 3, "first success")
    store.set_note(a, 1, "spray starts")

    res = store.list_row_notes()

    assert res["total"] == 3
    # Table then line: the order the tables are listed in and the order the
    # rows are read in — never insertion order, which nothing records.
    assert [(n["source_id"], n["rid"], n["note"], n["source_name"]) for n in res["notes"]] == [
        (a, 1, "spray starts", "security.csv"),
        (a, 3, "first success", "security.csv"),
        (b, 2, "staged here", "mft.csv"),
    ]


def test_the_listing_calls_a_table_what_the_analyst_renamed_it(store, write_csv):
    a, _ = _two_tables(store, write_csv)
    store.set_note(a, 2, "pivot")
    store.set_source_nickname(a, "WKSTN-4471 Security")

    assert store.list_row_notes()["notes"][0]["source_name"] == "WKSTN-4471 Security"


def test_clearing_a_note_takes_it_out_of_the_listing(store, write_csv):
    a, _ = _two_tables(store, write_csv)
    store.set_note(a, 2, "wrong row")
    store.set_note(a, 2, "   ")   # the detail pane's empty box: a delete

    assert store.list_row_notes() == {"total": 0, "notes": []}


def test_a_table_removed_from_the_case_takes_its_notes_with_it(store, write_csv):
    a, b = _two_tables(store, write_csv)
    store.set_note(a, 1, "on the table being dropped")
    store.set_note(b, 1, "on the table that stays")
    store.drop_source(a)

    res = store.list_row_notes()
    # Both halves: the listing joins to `sources`, so a note whose table is
    # gone can neither be listed unnameably nor counted into the total.
    assert res["total"] == 1
    assert [n["source_id"] for n in res["notes"]] == [b]


def test_more_notes_than_the_listing_shows_are_counted_anyway(store, write_csv):
    a, _ = _two_tables(store, write_csv)
    for rid in (1, 2, 3):
        store.set_note(a, rid, f"note {rid}")

    res = store.list_row_notes(limit=2)

    assert res["total"] == 3            # what the heading says
    assert [n["rid"] for n in res["notes"]] == [1, 2]   # what the strip shows


def test_a_note_taken_on_a_merged_row_is_listed_under_its_member(store, write_csv):
    """Invariant #9: a merge has no rows of its own — a note on one is
    written against the member it came from, like a tag. The listing has to
    name that member, because its (source_id, rid) is what opens the row."""
    a, b = _two_tables(store, write_csv)
    store.create_merge("everything", [a, b])
    store.set_note(b, 1, "seen through the merge")

    res = store.list_row_notes()

    assert [(n["source_id"], n["source_name"]) for n in res["notes"]] == [(b, "mft.csv")]


def test_the_route_answers_the_whole_case(client, store, write_csv):
    a, b = _two_tables(store, write_csv)
    store.set_note(a, 1, "one")
    store.set_note(b, 2, "two")

    body = client.get("/api/row_notes").json()

    assert body["total"] == 2
    assert [n["note"] for n in body["notes"]] == ["one", "two"]
    assert client.get("/api/row_notes?limit=1").json() == {
        "total": 2,
        "notes": [{"source_id": a, "rid": 1, "note": "one", "source_name": "security.csv"}],
    }
