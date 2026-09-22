"""What the whole-view tag hotkey asks before it decides.

Shift+<tag key> used to mean one thing: tag every row in this view. Press
it twice and the second press was a confirm dialog for a no-op. It reads
the coverage first now — anything untagged means tag the lot, everything
already tagged means take it off — so a repeat press undoes the press
before it, and the confirm can say which of the two it is about to do.

The numbers have to be right for all three kinds of view handle, since
the same keystroke works on a filtered view, an unfiltered one (which is
never materialised — invariant #2's carve-out) and inside an expanded
group.
"""

from __future__ import annotations

import pytest

from winnow.store import Store


@pytest.fixture
def case(tmp_path):
    p = tmp_path / "cov.csv"
    p.write_text("Host,EventId\n" + "".join(f"H{i % 2},{4624 + (i % 3)}\n" for i in range(12)),
                 encoding="utf-8")
    store = Store(str(tmp_path / "case.db"))
    try:
        src = store.ingest_csv(str(p))
        tag = store.upsert_tag(None, "Reviewed", "#ff0000", "1")
        yield store, src, tag
    finally:
        store.close()


def _view(store, src, **spec):
    return store.build_view(src["id"], {"filters": [], "sort": [], **spec})


def test_an_untouched_view_is_all_rows_and_nothing_tagged(case):
    store, src, tag = case
    v = _view(store, src)
    assert store.tag_coverage_in_view(v["view_id"], tag["id"]) == {"rows": 12, "tagged": 0}


def test_coverage_follows_the_filter_not_the_table(case):
    store, src, tag = case
    # Tag the whole table, then ask about a view of half of it: the answer
    # is about the view, which is what "every row in this view" means.
    whole = _view(store, src)
    store.tag_view(whole["view_id"], tag["id"], True)
    half = _view(store, src, filters=[{"column": "Host", "op": "equals", "value": "H0"}])
    cov = store.tag_coverage_in_view(half["view_id"], tag["id"])
    assert cov == {"rows": 6, "tagged": 6}

    # …and the reverse. Untagging inside the filtered view leaves the
    # rows outside it tagged, so the whole-table view is no longer covered
    # and the next press there tags rather than untags — which is the
    # decision this number exists to make.
    store.tag_view(half["view_id"], tag["id"], False)
    whole2 = _view(store, src)
    cov = store.tag_coverage_in_view(whole2["view_id"], tag["id"])
    assert cov == {"rows": 12, "tagged": 6}


def test_an_unfiltered_view_is_not_materialised_and_still_answers(case):
    """A view with no filter and no sort is the virtual-root carve-out —
    there is no v.view_N to join, and the count comes from the source."""
    store, src, tag = case
    v = _view(store, src)
    assert v["kind"] == "root_virtual"
    store.tag_view(v["view_id"], tag["id"], True)
    assert store.tag_coverage_in_view(v["view_id"], tag["id"]) == {"rows": 12, "tagged": 12}


def test_the_second_press_takes_the_tag_off_exactly_the_rows_it_put_it_on(case):
    """The round trip the hotkey performs, one press at a time, over a
    view that is part of a larger table. The rows outside it must not
    move — that is the difference between an untag and a mistake."""
    store, src, tag = case
    outside = _view(store, src, filters=[{"column": "Host", "op": "equals", "value": "H1"}])
    store.tag_view(outside["view_id"], tag["id"], True)

    view = _view(store, src, filters=[{"column": "Host", "op": "equals", "value": "H0"}])
    cov = store.tag_coverage_in_view(view["view_id"], tag["id"])
    assert cov["tagged"] < cov["rows"]          # so the press tags
    store.tag_view(view["view_id"], tag["id"], True)

    view = _view(store, src, filters=[{"column": "Host", "op": "equals", "value": "H0"}])
    cov = store.tag_coverage_in_view(view["view_id"], tag["id"])
    assert cov["tagged"] == cov["rows"]         # so the next press untags
    res = store.tag_view(view["view_id"], tag["id"], False)
    assert res["changed"] == 6

    # H0 is clear, H1 — tagged before any of this — is untouched.
    view = _view(store, src, filters=[{"column": "Host", "op": "equals", "value": "H0"}])
    assert store.tag_coverage_in_view(view["view_id"], tag["id"])["tagged"] == 0
    other = _view(store, src, filters=[{"column": "Host", "op": "equals", "value": "H1"}])
    assert store.tag_coverage_in_view(other["view_id"], tag["id"]) == {"rows": 6, "tagged": 6}


def test_an_expired_view_is_the_usual_keyerror(case):
    store, src, tag = case
    with pytest.raises(KeyError):
        store.tag_coverage_in_view("view_does_not_exist", tag["id"])


def test_the_route_answers_409_for_an_expired_view(client, store):
    r = client.get("/api/tag_view_coverage?view_id=view_nope&tag_id=1")
    assert r.status_code == 409
