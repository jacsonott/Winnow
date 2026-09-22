"""Dashboard results kept in the case file.

A board was a pure function re-evaluated on every mount: the shipped KAPE
board issued 26 widget queries when it was opened, when a card was dragged
and when any other card was edited, and "opened" mostly means "reopened
this case tomorrow". Results are now cached per widget, so what is under
test here is when a cached answer is served, when it is thrown away, and
when it is served with a warning:

  * served     — nothing about the widget or the case has changed;
  * thrown away — the widget's question (its source + query) changed, or
                  the widget or the board is gone;
  * served stale — rows were imported or tagged since it ran. The number
                  was true and is dated; hiding it would trade a true-an-
                  hour-ago number for no number at all.

Widget identity is the thing everything else hangs off, so it is pinned
first: widgets were a JSON list identified by position, and a drag-reorder
rewrote the list.

"Served stale" has a test per writer rather than one for the mechanism.
Half of data_generation is derived from the `sources` table and cannot be
forgotten; the other half is a counter every tag and watchlist write has
to remember to bump, and each of the writers that bypasses the tag delta
path — a tag deleted, a session restored, the case cleared for a second
pass — was its own way of being told a number was current when it was not.
"""

from __future__ import annotations

import json

import pytest

from winnow.store import Store

ROWS = [["Host", "EventId"], ["WKS07", "4625"], ["WKS01", "4624"], ["WKS07", "4625"]]
MORE = [["Host", "EventId"], ["SRV1", "4624"]]


def _widget(title, sql, **extra):
    w = {"title": title, "source": "sql", "render": "stat", "query": {"sql": sql}}
    w.update(extra)
    return w


def _board(store, sid, **extra):
    return store.create_dashboard("Triage", [
        _widget("Rows", f"SELECT COUNT(*) FROM src_{sid}"),
        _widget("Failed", f"SELECT COUNT(*) FROM src_{sid} WHERE EventId='4625'"),
        {"title": "Tagged", "source": "tags", "render": "stat", **extra},
    ])["id"]


@pytest.fixture
def case(store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "e.csv"), name="e", build_fts=False)["id"]
    return store, sid


# --------------------------------------------------------------- identity

def test_every_widget_written_to_a_board_gets_an_id(case):
    store, sid = case
    did = _board(store, sid)
    ids = [w["id"] for w in store.get_dashboard(did)]
    assert all(ids) and len(set(ids)) == 3


def test_two_identical_widgets_do_not_share_an_id(store):
    """They would share a cached result, and marking one "run every time"
    would quietly refresh the other."""
    same = _widget("Rows", "SELECT 1")
    did = store.create_dashboard("Twins", [dict(same), dict(same)])["id"]
    a, b = store.get_dashboard(did)
    assert a["id"] != b["id"]


def test_a_board_written_before_ids_existed_reads_the_same_ids_twice(store):
    """The back-fill has to be derived, not minted per read: a case nobody
    has edited since the upgrade would otherwise get a fresh key on every
    open and never see a cache hit — which is the exact board this change
    is for."""
    raw = json.dumps([_widget("Rows", "SELECT 1"), _widget("Cols", "SELECT 2")])
    with store.lock, store.db:
        store.db.execute("INSERT INTO dashboards(name, widgets, pos) VALUES ('Old', ?, 0)", (raw,))
        did = store.db.execute("SELECT id FROM dashboards WHERE name='Old'").fetchone()["id"]
    first = [w["id"] for w in store.get_dashboard(did)]
    second = [w["id"] for w in store.get_dashboard(did)]
    assert first == second and all(first)


def test_a_reorder_keeps_every_widgets_id(case):
    store, sid = case
    did = _board(store, sid)
    before = store.get_dashboard(did)
    store.set_dashboard_widgets(did, [before[2], before[0], before[1]])
    assert [w["id"] for w in store.get_dashboard(did)] == [before[2]["id"], before[0]["id"], before[1]["id"]]


def test_minting_does_not_stamp_the_callers_widgets(store):
    """A plugin's registered board and a profile's are shared objects,
    copied into every case that asks for them."""
    shipped = [_widget("Rows", "SELECT 1")]
    store.create_dashboard("A", shipped)
    store.upsert_dashboard_by_name("B", shipped)
    assert "id" not in shipped[0]


# ------------------------------------------------------------ caching them

def test_a_run_is_cached_and_served_back(case):
    store, sid = case
    did = _board(store, sid)
    wid = store.get_dashboard(did)[0]["id"]
    store.cache_widget_result(did, wid, {"columns": ["n"], "rows": [[3]]}, 120)
    hit = store.get_dashboard_cache(did)[wid]
    assert hit["payload"]["rows"] == [[3]]
    assert hit["elapsed_ms"] == 120 and hit["ran_at"] and hit["stale"] is False


def test_a_result_for_a_widget_that_is_not_on_the_board_is_not_stored(case):
    """The editor previewing an unsaved draft, or a widget removed while
    its query was in flight — filing that under a key nothing matches is
    worse than not filing it."""
    store, sid = case
    did = _board(store, sid)
    assert store.cache_widget_result(did, "w-nope", {"rows": [[1]]}, 1) is None
    assert store.get_dashboard_cache(did) == {}


def test_editing_a_widgets_query_drops_its_result_and_only_its_result(case):
    store, sid = case
    did = _board(store, sid)
    store.refresh_dashboard(did)
    widgets = store.get_dashboard(did)
    kept = [w["id"] for w in widgets[1:]]
    widgets[0]["query"]["sql"] = f"SELECT COUNT(DISTINCT Host) FROM src_{sid}"
    store.set_dashboard_widgets(did, widgets)
    cache = store.get_dashboard_cache(did)
    assert widgets[0]["id"] not in cache
    assert sorted(cache) == sorted(kept)


def test_retitling_a_widget_keeps_its_result(case):
    """The fingerprint is the question the widget asks, not the whole dict:
    a twelve-second GROUP BY should not be thrown away because someone
    fixed a typo in the card's heading."""
    store, sid = case
    did = _board(store, sid)
    store.refresh_dashboard(did)
    widgets = store.get_dashboard(did)
    widgets[0]["title"] = "Row count"
    widgets[0]["span"] = 2
    store.set_dashboard_widgets(did, widgets)
    assert widgets[0]["id"] in store.get_dashboard_cache(did)


def test_removing_a_widget_drops_its_result(case):
    store, sid = case
    did = _board(store, sid)
    store.refresh_dashboard(did)
    widgets = store.get_dashboard(did)
    gone = widgets.pop(1)["id"]
    store.set_dashboard_widgets(did, widgets)
    assert gone not in store.get_dashboard_cache(did)
    rows = store.db.execute(
        "SELECT COUNT(*) FROM dashboard_widget_cache WHERE dashboard_id=? AND widget_id=?",
        (did, gone)).fetchone()[0]
    assert rows == 0, "left behind in the table, not just filtered out of the read"


def test_deleting_a_board_takes_its_results_with_it(case):
    """SQLite hands the id out again, so a leftover row would show the
    previous board's numbers on the next one."""
    store, sid = case
    did = _board(store, sid)
    store.refresh_dashboard(did)
    store.delete_dashboard(did)
    assert store.db.execute(
        "SELECT COUNT(*) FROM dashboard_widget_cache WHERE dashboard_id=?", (did,)).fetchone()[0] == 0


def test_replacing_a_board_by_name_drops_the_results_that_no_longer_apply(case):
    """How a profile applies its board (and how a plugin re-adds one)."""
    store, sid = case
    did = _board(store, sid)
    store.refresh_dashboard(did)
    before = store.get_dashboard(did)
    store.upsert_dashboard_by_name("Triage", [
        _widget("Rows", f"SELECT COUNT(*) FROM src_{sid}"),          # same question
        _widget("Hosts", f"SELECT COUNT(DISTINCT Host) FROM src_{sid}"),   # new one
    ])
    cache = store.get_dashboard_cache(did)
    assert list(cache) == [before[0]["id"]]


# --------------------------------------------------------------- refreshing

def test_refresh_runs_every_widget_and_stamps_them(case):
    store, sid = case
    did = _board(store, sid)
    out = store.refresh_dashboard(did)
    ids = [w["id"] for w in store.get_dashboard(did)]
    assert sorted(out["results"]) == sorted(ids)
    assert out["results"][ids[1]]["payload"]["rows"] == [[2]]
    assert sorted(store.get_dashboard_cache(did)) == sorted(ids)


def test_refresh_can_name_one_widget(case):
    store, sid = case
    did = _board(store, sid)
    wid = store.get_dashboard(did)[1]["id"]
    out = store.refresh_dashboard(did, wid)
    assert list(out["results"]) == [wid]
    assert list(store.get_dashboard_cache(did)) == [wid]
    with pytest.raises(KeyError):
        store.refresh_dashboard(did, "w-nope")


def test_a_widget_that_fails_is_reported_and_not_cached(case):
    """Caching an error would make the card retry never — and a failing
    widget is usually "that table is not in this case YET"."""
    store, sid = case
    did = store.create_dashboard("Broken", [
        _widget("Ok", f"SELECT COUNT(*) FROM src_{sid}"),
        _widget("Missing", "SELECT COUNT(*) FROM {{registry}}"),
    ])["id"]
    out = store.refresh_dashboard(did)
    ids = [w["id"] for w in store.get_dashboard(did)]
    assert "error" in out["results"][ids[1]]
    assert list(store.get_dashboard_cache(did)) == [ids[0]]


# ---------------------------------------------------------------- staleness

def test_an_import_marks_the_board_stale_without_hiding_it(case, write_csv):
    store, sid = case
    did = _board(store, sid)
    store.refresh_dashboard(did)
    wid = store.get_dashboard(did)[0]["id"]
    assert store.get_dashboard_cache(did)[wid]["stale"] is False
    store.ingest_csv(write_csv(MORE, "more.csv"), name="more", build_fts=False)
    hit = store.get_dashboard_cache(did)[wid]
    assert hit["stale"] is True
    assert hit["payload"]["rows"] == [[3]], "the number it was is still shown, dated"


def test_a_tag_write_marks_the_board_stale_and_so_does_undoing_it(case):
    store, sid = case
    did = _board(store, sid)
    store.refresh_dashboard(did)
    wid = store.get_dashboard(did)[0]["id"]
    store.set_tags(sid, [1], store.list_tags()[0]["id"], True)
    assert store.get_dashboard_cache(did)[wid]["stale"] is True
    store.refresh_dashboard(did)
    assert store.get_dashboard_cache(did)[wid]["stale"] is False
    store.undo_last_tag_change()
    assert store.get_dashboard_cache(did)[wid]["stale"] is True


def test_re_tagging_rows_that_already_carry_the_tag_changes_nothing(case):
    """The generation follows what row_tags actually did, not what was
    asked for — the same delta _apply_tag_change records for undo."""
    store, sid = case
    tag = store.list_tags()[0]["id"]
    store.set_tags(sid, [1], tag, True)
    did = _board(store, sid)
    store.refresh_dashboard(did)
    wid = store.get_dashboard(did)[0]["id"]
    store.set_tags(sid, [1], tag, True)
    assert store.get_dashboard_cache(did)[wid]["stale"] is False


def test_clearing_the_case_for_a_second_pass_marks_the_board_stale(case):
    """start_new_session deletes every row_tags row by hand — invariant #7's
    documented exception, so it never reaches the tag delta path. A tag
    widget cached before it says "3 tagged" over a case with none."""
    store, sid = case
    tag = store.list_tags()[0]["id"]
    store.set_tags(sid, [1, 2, 3], tag, True)
    did = _board(store, sid)
    store.refresh_dashboard(did)
    tagged = store.get_dashboard(did)[2]["id"]
    assert store.get_dashboard_cache(did)[tagged]["payload"]["total"] == 3
    store.start_new_session()
    hit = store.get_dashboard_cache(did)[tagged]
    assert hit["stale"] is True
    assert hit["payload"]["total"] == 3, "the number it counted is still shown, dated"


def test_deleting_a_tag_marks_the_board_stale(case):
    """It drops every assignment of that tag with it, straight out of
    row_tags."""
    store, sid = case
    tag = store.list_tags()[0]["id"]
    store.set_tags(sid, [1, 2], tag, True)
    did = _board(store, sid)
    store.refresh_dashboard(did)
    tagged = store.get_dashboard(did)[2]["id"]
    store.delete_tag(tag)
    assert store.get_dashboard_cache(did)[tagged]["stale"] is True


def test_naming_a_new_tag_marks_the_board_stale(case):
    """A tags widget lists one row per DEFINITION, so its answer changes
    before a single row carries the tag."""
    store, sid = case
    did = _board(store, sid)
    store.refresh_dashboard(did)
    tagged = store.get_dashboard(did)[2]["id"]
    store.upsert_tag(None, "Lateral movement", "#8844cc", None)
    assert store.get_dashboard_cache(did)[tagged]["stale"] is True


def test_recolouring_a_tag_leaves_the_board_alone(case):
    """The counter follows what the write changed. A tags widget is names
    and counts; the palette is neither."""
    store, sid = case
    tag = store.list_tags()[0]
    did = _board(store, sid)
    store.refresh_dashboard(did)
    tagged = store.get_dashboard(did)[2]["id"]
    store.upsert_tag(tag["id"], tag["name"], "#123456", tag.get("hotkey"))
    assert store.get_dashboard_cache(did)[tagged]["stale"] is False


def test_restoring_a_saved_session_marks_the_board_stale(case):
    """Loading a session INSERTs row_tags wholesale — the tags of another
    pass over the same evidence, arriving without a delta behind them."""
    store, sid = case
    tag = store.list_tags()[0]["id"]
    store.set_tags(sid, [1, 2, 3], tag, True)
    store.save_session("pass1")
    store.start_new_session()
    did = _board(store, sid)
    store.refresh_dashboard(did)
    tagged = store.get_dashboard(did)[2]["id"]
    assert store.get_dashboard_cache(did)[tagged]["payload"]["total"] == 0
    store.load_session("pass1")
    assert store.get_dashboard_cache(did)[tagged]["stale"] is True


def test_a_watchlist_scan_that_finds_hits_marks_the_board_stale(case):
    """A watchlist widget's number is the sum of the hit counts, and a scan
    moves it with nothing in `sources` changing."""
    store, sid = case
    did = store.create_dashboard("IOCs", [
        {"title": "Watchlist hits", "source": "watchlist", "render": "stat"}])["id"]
    store.add_indicator("WKS07")
    store.refresh_dashboard(did)
    wid = store.get_dashboard(did)[0]["id"]
    assert store.get_dashboard_cache(did)[wid]["payload"]["total"] == 0
    store.scan_all()
    assert store.get_dashboard_cache(did)[wid]["stale"] is True


def test_re_scanning_an_unchanged_case_leaves_the_board_alone(case):
    """The counter follows what the hits DID, not that a scan ran: a board
    that reddened every time the watchlist re-scanned — which happens after
    every import — would teach the analyst to ignore the mark."""
    store, sid = case
    did = store.create_dashboard("IOCs", [
        {"title": "Watchlist hits", "source": "watchlist", "render": "stat"}])["id"]
    store.add_indicator("WKS07")
    store.scan_all()
    store.refresh_dashboard(did)
    wid = store.get_dashboard(did)[0]["id"]
    store.scan_all()
    assert store.get_dashboard_cache(did)[wid]["stale"] is False


def test_dropping_an_indicator_marks_the_board_stale(case):
    store, sid = case
    did = store.create_dashboard("IOCs", [
        {"title": "Watchlist hits", "source": "watchlist", "render": "stat"}])["id"]
    ind = store.add_indicator("WKS07")
    store.scan_all()
    store.refresh_dashboard(did)
    wid = store.get_dashboard(did)[0]["id"]
    store.delete_indicator(ind["id"])
    assert store.get_dashboard_cache(did)[wid]["stale"] is True


def test_a_widget_returning_a_blob_is_cached_rather_than_failing(case):
    """A widget can ask for bytes — `unhex`, `randomblob`, a CAST — and
    json.dumps refuses them. Refusing to FILE a result is not a reason to
    fail a query that already answered, so the encode is lenient and the
    run is stored like any other."""
    store, sid = case
    did = store.create_dashboard("Blobs", [
        _widget("Bytes", "SELECT CAST('abc' AS BLOB) AS b"),
        _widget("Rows", f"SELECT COUNT(*) FROM src_{sid}")])["id"]
    out = store.refresh_dashboard(did)
    assert not any("error" in r for r in out["results"].values())
    hits = store.get_dashboard_cache(did)
    assert len(hits) == 2, "one unencodable widget must not take the board's cache with it"


def test_a_merge_widget_goes_stale_when_a_member_grows(store, write_csv):
    """Merge parity for the cache. Dashboards do not offer merges in the
    editor (CLAUDE.md invariant #9's exception list), but hand-written SQL
    reaches one through the pane connection's merge_<id> view — so a
    cached merge result has to date itself off its members' rows like any
    other."""
    a = store.ingest_csv(write_csv(ROWS, "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(MORE, "b.csv"), name="b", build_fts=False)["id"]
    mid = store.create_merge("both", [a, b])["id"]
    did = store.create_dashboard("Merged", [
        _widget("All rows", f"SELECT COUNT(*) FROM merge_{abs(mid)}")])["id"]
    out = store.refresh_dashboard(did)
    wid = store.get_dashboard(did)[0]["id"]
    assert out["results"][wid]["payload"]["rows"] == [[4]]
    assert store.get_dashboard_cache(did)[wid]["stale"] is False
    store.ingest_csv(write_csv(MORE, "c.csv"), name="c", build_fts=False)
    assert store.get_dashboard_cache(did)[wid]["stale"] is True


# ------------------------------------------------------------- portability

def test_results_do_not_travel_with_copy_sources_to(store, write_csv, tmp_path):
    """Decided and pinned: a save-as / quick-look copy moves EVIDENCE —
    rows, tags, notes, layouts, saved views — and no board goes with it, so
    no board's numbers do either. The target case is not a place the
    sender's "Failed logons: 651" belongs.

    (A case file handed over whole is the other story: the board and its
    cache are both in the .db, and every card is dated.)"""
    sid = store.ingest_csv(write_csv(ROWS, "e.csv"), name="e", build_fts=False)["id"]
    did = _board(store, sid)
    store.refresh_dashboard(did)
    target_path = str(tmp_path / "real.db")
    Store(target_path).close()
    store.copy_sources_to(target_path, [sid])
    t = Store(target_path)
    try:
        assert t.list_dashboards() == []
        assert t.db.execute("SELECT COUNT(*) FROM dashboard_widget_cache").fetchone()[0] == 0
    finally:
        t.close()


# ------------------------------------------------------------------ routes

def test_the_board_route_hands_over_definitions_and_results_together(client, store, write_csv):
    """One request, because the alternative is the thing being fixed: an
    empty grid followed by one query per card."""
    sid = store.ingest_csv(write_csv(ROWS, "e.csv"), name="e", build_fts=False)["id"]
    did = _board(store, sid)
    body = client.get(f"/api/dashboards/{did}").json()
    assert body["cache"] == {}, "nothing has run yet"
    wid = body["widgets"][0]["id"]
    r = client.post("/api/dashboard/widget/preview", json={
        "source": "sql", "query": {"sql": f"SELECT COUNT(*) FROM src_{sid}"},
        "dashboard_id": did, "widget_id": wid})
    assert r.status_code == 200 and r.json()["ran_at"]
    again = client.get(f"/api/dashboards/{did}").json()
    assert again["cache"][wid]["payload"]["rows"] == [[3]]
    assert again["cache"][wid]["stale"] is False


def test_a_preview_with_no_board_caches_nothing(client, store, write_csv):
    """What the widget editor's Preview sends. A Preview that answered from
    the cache would not be previewing anything."""
    sid = store.ingest_csv(write_csv(ROWS, "e.csv"), name="e", build_fts=False)["id"]
    did = _board(store, sid)
    client.post("/api/dashboard/widget/preview",
                json={"source": "sql", "query": {"sql": f"SELECT COUNT(*) FROM src_{sid}"}})
    assert client.get(f"/api/dashboards/{did}").json()["cache"] == {}


def test_a_preview_the_cache_cannot_file_still_answers(client, store, write_csv, monkeypatch):
    """Filing the result is best-effort, and best-effort has to mean any
    failure, not a list of the ones that were thought of: the analyst
    asked for a number and the query produced one, so nothing that happens
    on the way to the filing cabinet may turn that 200 into a 500."""
    sid = store.ingest_csv(write_csv(ROWS, "e.csv"), name="e", build_fts=False)["id"]
    did = _board(store, sid)
    wid = store.get_dashboard(did)[0]["id"]

    def boom(*a, **k):
        raise TypeError("Object of type bytes is not JSON serializable")

    monkeypatch.setattr(type(store), "cache_widget_result", boom)
    r = client.post("/api/dashboard/widget/preview", json={
        "source": "sql", "query": {"sql": f"SELECT COUNT(*) FROM src_{sid}"},
        "dashboard_id": did, "widget_id": wid})
    assert r.status_code == 200
    assert r.json()["rows"] == [[3]]
    assert "ran_at" not in r.json(), "nothing was filed, so nothing is stamped"


def test_saving_widgets_hands_back_the_ids_the_store_minted(client, store, write_csv):
    """A card the analyst just added has no cached result to find until the
    client knows what that card is called."""
    sid = store.ingest_csv(write_csv(ROWS, "e.csv"), name="e", build_fts=False)["id"]
    did = client.post("/api/dashboards", json={"name": "New"}).json()["id"]
    r = client.post(f"/api/dashboards/{did}",
                    json={"widgets": [_widget("Rows", f"SELECT COUNT(*) FROM src_{sid}")]})
    assert r.status_code == 200
    assert r.json()["widgets"][0]["id"] == store.get_dashboard(did)[0]["id"]
    # A rename carries no widgets, so it carries no widget list back.
    assert "widgets" not in client.post(f"/api/dashboards/{did}", json={"name": "Renamed"}).json()


def test_the_refresh_route_reruns_and_404s_on_a_board_that_is_gone(client, store, write_csv):
    sid = store.ingest_csv(write_csv(ROWS, "e.csv"), name="e", build_fts=False)["id"]
    did = _board(store, sid)
    out = client.post(f"/api/dashboards/{did}/refresh", json={}).json()
    wid = store.get_dashboard(did)[0]["id"]
    assert out["results"][wid]["payload"]["rows"] == [[3]]
    one = client.post(f"/api/dashboards/{did}/refresh", json={"widget_id": wid}).json()
    assert list(one["results"]) == [wid]
    assert client.post("/api/dashboards/9999/refresh", json={}).status_code == 404


# ------------------------------------------------------------ shipped board

def test_the_shipped_kape_board_keeps_its_cheap_widgets_live(client):
    """The card that counts the analyst's OWN work — numbers that are
    wrong to show a minute old — and nothing that scans the log.

    The two counts are cells of one card now rather than two cards, which
    is what keeps them live at all: everything else on the board is a
    scan, so a live flag on any of it would put the cost back that the
    cache took away."""
    profiles = client.get("/api/plugin_bundles").json()
    kape = next(p for p in profiles if p["name"] == "KAPE triage")
    live = [w for w in kape["dashboard"] if w.get("live")]
    assert [w["title"] for w in live] == ["Findings"]
    assert [c["source"] for c in live[0]["cells"]] == ["watchlist", "tags"]
    # Nothing that reads a log runs on every open — not as a widget, and
    # not as a cell of one.
    for w in kape["dashboard"]:
        if not w.get("live"):
            continue
        for c in w.get("cells") or [w]:
            assert (c.get("source") or "sql") != "sql", (w["title"], c.get("label"))
