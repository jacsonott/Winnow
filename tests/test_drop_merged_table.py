"""Deleting a table that a merge is built on is refused.

`merges.source_ids` is a JSON array of real source ids, and `drop_source`
cleans every other id-keyed sidecar but deliberately not this one: a
member that vanishes surfaces as a KeyError when the merge is opened,
rather than as a merge that silently changed shape.

That is the right way to REPORT a missing member and the wrong thing to
let an analyst do by accident, because source ids are reused. Dropping a
member does not merely break the merge — it arms it: the next file
imported takes the freed id and becomes part of a merge nobody added it
to. That is the case this file pins, because it is silent, it is
evidence, and nothing else in the app would ever mention it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

HEADERS = {"X-Timeline-Lite-Client": "1"}

ROWS_A = [["When", "Who"], ["2024-01-01 10:00", "alice"]]
ROWS_B = [["When", "Who"], ["2024-01-02 10:00", "carol"]]


@pytest.fixture
def merged(store, write_csv):
    a = store.ingest_csv(write_csv(ROWS_A, "a.csv"), build_fts=False)["id"]
    b = store.ingest_csv(write_csv(ROWS_B, "b.csv"), build_fts=False)["id"]
    merge = store.create_merge("Both", [a, b])
    return a, b, merge


def test_a_member_cannot_be_dropped(store, merged):
    a_id, _b, _m = merged
    with pytest.raises(ValueError) as e:
        store.drop_source(a_id)
    # The message has to name the merge: "it's in a merge" sends the
    # analyst hunting through a sidebar for which one.
    assert "Both" in str(e.value)
    assert store.get_source(a_id)["id"] == a_id, "the refusal must not half-delete it"


def test_the_id_reuse_this_prevents(store, merged):
    """The reason it is a refusal and not a warning. Without the guard the
    freed id is handed to the next import, which then belongs to a merge
    it was never added to — and nothing says so."""
    a_id, _b, m = merged
    members = store.merges_using(a_id)
    assert [x["name"] for x in members] == ["Both"]

    # Deleting the merge first is the supported order, and it leaves the
    # tables alone — which is what makes refusing a reasonable answer.
    store.delete_merge(-m["id"])   # the merges table keys on the POSITIVE id
    assert store.merges_using(a_id) == []
    store.drop_source(a_id)
    assert all(x["id"] != a_id for x in store.list_sources())


def test_a_table_in_no_merge_is_unaffected(store, merged):
    _a, _b, m = merged
    store.delete_merge(-m["id"])   # the merges table keys on the POSITIVE id
    other = store.list_sources()[0]["id"]
    store.drop_source(other)
    assert all(x["id"] != other for x in store.list_sources())


def test_the_route_answers_409_and_says_which_merge(store, merged, monkeypatch):
    """409 rather than 400: the request is well-formed and will succeed
    once the merge above it is gone."""
    import server as server_mod

    a_id, _b, _m = merged
    monkeypatch.setattr(server_mod, "store", lambda: store)
    c = TestClient(server_mod.app)
    r = c.delete(f"/api/source/{a_id}", headers=HEADERS)
    assert r.status_code == 409, r.text
    assert "Both" in r.json()["detail"]
