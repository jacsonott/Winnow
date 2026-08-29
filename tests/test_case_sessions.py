"""Named sessions stored IN the case file, and diffing two of them.

The diff is the load-bearing part: it exists so an analyst's work can be
QC'd against a reviewer's, which means it has to survive the two things
that are always true of two separate cases — different source ids, and
different tag ids for the same tag name.
"""

from __future__ import annotations

import json

import pytest

ROWS = [["Host", "User"], ["h1", "alice"], ["h2", "bob"], ["h3", "carol"], ["h4", "dave"]]


def _case(store, write_csv, name="evidence.csv"):
    return store.ingest_csv(write_csv(ROWS, name), name=name, build_fts=False)["id"]


def _tag(store, sid, rids, tag_name):
    tag = next((t for t in store.list_tags() if t["name"] == tag_name), None)
    if tag is None:
        tag = store.upsert_tag(None, tag_name, "#ff0000", None)
    store.set_tags(sid, list(rids), tag["id"], True)
    return tag["id"]


def test_a_saved_session_lives_in_the_case_file(store, write_csv, case_path):
    sid = _case(store, write_csv)
    _tag(store, sid, [1, 2], "TA")
    rec = store.save_session("first pass")

    assert rec["name"] == "first pass"
    assert rec["tagged_rows"] == 2
    assert [s["name"] for s in store.list_sessions()] == ["first pass"]

    # In the case file — so it travels when the .db is copied, with no
    # sessions/ directory to leave behind.
    row = store.db.execute("SELECT payload FROM sessions WHERE name='first pass'").fetchone()
    assert json.loads(row["payload"])["format"] == "winnow-case-session/1"


def test_saving_the_same_name_replaces_rather_than_accumulates(store, write_csv):
    sid = _case(store, write_csv)
    _tag(store, sid, [1], "TA")
    store.save_session("pass")
    _tag(store, sid, [2, 3], "TA")
    rec = store.save_session("pass")
    assert len(store.list_sessions()) == 1
    assert rec["tagged_rows"] == 3


def test_a_blank_slate_without_losing_the_saved_work(store, write_csv):
    """The workflow this exists for: save, clear, work again, go back."""
    sid = _case(store, write_csv)
    ta = _tag(store, sid, [1, 2], "TA")
    store.save_session("first pass")

    store.set_tags(sid, [1, 2], ta, False)          # blank slate
    assert store.tag_counts(sid)["counts"].get(str(ta), 0) == 0
    _tag(store, sid, [4], "TA")                      # a different conclusion

    store.load_session("first pass", merge=False)    # and back
    tagged = {r["rid"] for r in store.db.execute(
        "SELECT rid FROM row_tags WHERE source_id=?", (sid,))}
    assert tagged == {1, 2}


def test_diff_reports_what_a_reviewer_added_and_removed(store, write_csv):
    sid = _case(store, write_csv)
    _tag(store, sid, [1, 2], "TA")
    store.save_session("analyst")

    ta = next(t["id"] for t in store.list_tags() if t["name"] == "TA")
    store.set_tags(sid, [2], ta, False)   # reviewer disagrees with row 2
    _tag(store, sid, [3], "TA")           # and finds one the analyst missed
    store.save_session("reviewer")

    d = store.diff_sessions("analyst", "reviewer")
    assert [r["rid"] for r in d["added"]] == [3], "the reviewer's new finding"
    assert [r["rid"] for r in d["removed"]] == [2], "what the reviewer dropped"
    assert d["counts"]["changed"] == 0
    assert d["shared_sources"] and not d["only_left_sources"]


def test_diff_against_the_live_case(store, write_csv):
    """The usual review shape: a session that arrived from someone else,
    against what the reviewer currently has."""
    sid = _case(store, write_csv)
    _tag(store, sid, [1], "TA")
    store.save_session("handover")
    _tag(store, sid, [4], "Suspicious")

    d = store.diff_sessions("handover", store.LIVE_SESSION)
    assert [r["rid"] for r in d["added"]] == [4]
    assert d["added"][0]["right"] == ["Suspicious"]


def test_a_row_tagged_differently_is_changed_not_added_and_removed(store, write_csv):
    sid = _case(store, write_csv)
    _tag(store, sid, [1], "TA")
    store.save_session("before")
    ta = next(t["id"] for t in store.list_tags() if t["name"] == "TA")
    store.set_tags(sid, [1], ta, False)
    _tag(store, sid, [1], "Benign")       # same row, different conclusion
    store.save_session("after")

    d = store.diff_sessions("before", "after")
    assert d["counts"] == {"added": 0, "removed": 0, "changed": 1, "note_changes": 0}
    assert d["changed"][0] == {"source": "evidence.csv", "rid": 1,
                               "left": ["TA"], "right": ["Benign"]}


def test_diff_matches_tags_by_name_across_differently_numbered_cases(store, write_csv, tmp_path):
    """The reason this feature works at all. Two analysts' cases number
    their tags independently, so comparing tag_ids would report every row
    as different even when they agree completely."""
    sid = _case(store, write_csv)
    # A tag name that is NOT one of the seeded defaults (TA/Suspicious/
    # Benign all get the same ids in every case), created after fillers so
    # it lands on a high id here.
    for filler in ("Noise", "Later", "Other"):
        store.upsert_tag(None, filler, "#888888", None)
    _tag(store, sid, [1, 2], "Lateral movement")
    theirs = store.export_case_session()
    ours_ta = next(t["id"] for t in store.list_tags() if t["name"] == "Lateral movement")

    # A second case where the same tag NAME has a different id entirely.
    from winnow.store import Store
    other = Store(str(tmp_path / "other.db"))
    try:
        osid = _case(other, write_csv)
        their_ta = _tag(other, osid, [1, 2], "Lateral movement")
        assert their_ta != ours_ta, "fixture must actually differ, or this proves nothing"
        other.adopt_session("from colleague", theirs)
        d = other.diff_sessions("from colleague", other.LIVE_SESSION)
        # The two agree completely, so EVERY bucket must be empty. Checking
        # only added/removed would miss the id-comparison bug entirely: it
        # reports agreeing rows as "changed" (left {7}, right {4}), which is
        # just as wrong and just as useless to a reviewer.
        assert d["counts"] == {"added": 0, "removed": 0, "changed": 0,
                               "note_changes": 0}, d
    finally:
        other.close()


def test_an_adopted_session_travels_with_the_case(store, write_csv):
    """A session received as a file is recorded in the case, so passing the
    case on carries it — that is the whole point of moving them in here."""
    sid = _case(store, write_csv)
    _tag(store, sid, [1], "TA")
    handover = store.export_case_session()
    store.delete_session("x")  # no-op, must not raise

    rec = store.adopt_session("from colleague", handover)
    assert rec["origin"] == "imported"
    assert [s["origin"] for s in store.list_sessions()] == ["imported"]


def test_adopting_something_that_is_not_a_session_is_refused(store):
    with pytest.raises(ValueError, match="isn't a Winnow case session"):
        store.adopt_session("bad", {"format": "something-else", "sources": []})


def test_export_writes_the_hand_off_file(store, write_csv, tmp_path):
    sid = _case(store, write_csv)
    _tag(store, sid, [1], "TA")
    store.save_session("to send")
    out = tmp_path / "handover.winnow_case.json"

    res = store.export_session_file("to send", str(out))

    assert res["bytes"] > 0
    data = json.loads(out.read_text())
    assert data["format"] == "winnow-case-session/1"
    assert len(data["sources"]) == 1


def test_rename_and_delete(store, write_csv):
    _case(store, write_csv)
    store.save_session("typo")
    store.rename_session("typo", "first pass")
    assert [s["name"] for s in store.list_sessions()] == ["first pass"]
    with pytest.raises(KeyError):
        store.rename_session("nope", "x")
    store.save_session("second")
    with pytest.raises(ValueError, match="already exists"):
        store.rename_session("second", "first pass")
    store.delete_session("first pass")
    assert [s["name"] for s in store.list_sessions()] == ["second"]


def test_an_unnamed_session_is_refused(store, write_csv):
    _case(store, write_csv)
    with pytest.raises(ValueError, match="needs a name"):
        store.save_session("   ")


def test_missing_sessions_raise_rather_than_returning_nothing(store):
    with pytest.raises(KeyError, match="No session named"):
        store.get_session("never saved")
    with pytest.raises(KeyError):
        store.diff_sessions("never saved", store.LIVE_SESSION)


# --------------------------------------------------------------- HTTP layer

HEADERS = {"X-Timeline-Lite-Client": "1"}


def _client(store, monkeypatch):
    import server
    from fastapi.testclient import TestClient
    monkeypatch.setattr(server, "STORE", store)
    return TestClient(server.app)


def test_routes_save_list_diff_and_download(store, write_csv, monkeypatch):
    sid = _case(store, write_csv)
    _tag(store, sid, [1, 2], "TA")
    client = _client(store, monkeypatch)

    assert client.post("/api/case_sessions", json={"name": "analyst"},
                       headers=HEADERS).status_code == 200
    listed = client.get("/api/case_sessions", headers=HEADERS).json()["sessions"]
    assert [s["name"] for s in listed] == ["analyst"]

    _tag(store, sid, [3], "TA")
    d = client.get("/api/case_sessions/diff",
                   params={"left": "analyst", "right": store.LIVE_SESSION},
                   headers=HEADERS).json()
    assert [r["rid"] for r in d["added"]] == [3]

    dl = client.get("/api/case_sessions/analyst/download", headers=HEADERS)
    assert dl.status_code == 200
    assert "attachment" in dl.headers["content-disposition"]
    assert dl.json()["format"] == "winnow-case-session/1"


def test_routes_report_a_missing_session_as_404(store, monkeypatch):
    client = _client(store, monkeypatch)
    assert client.post("/api/case_sessions/nope/load", headers=HEADERS).status_code == 404
    assert client.get("/api/case_sessions/nope/download", headers=HEADERS).status_code == 404
    assert client.get("/api/case_sessions/diff", params={"left": "nope", "right": "__live__"},
                      headers=HEADERS).status_code == 404


def test_an_unnamed_session_is_a_400_not_a_500(store, monkeypatch):
    client = _client(store, monkeypatch)
    assert client.post("/api/case_sessions", json={"name": " "},
                       headers=HEADERS).status_code == 400
