"""Per-case UI state for a plugin's tab or panel (plugin_ui_state), the
store half and the two routes behind winnow.tabState.

A mount is torn down with no callback — there is no onDestroy in the tab
contract — so a plugin cannot flush anything on the way out and writes as
the analyst works instead. What that costs is covered here: the row has to
be in the CASE file (so it survives a close and travels with the .db),
keyed per mount (so a plugin's tab and its panel are not one namespace),
capped (so "save the definition" does not become "save the result"), and
untouched by the plugin being toggled off.
"""

from __future__ import annotations

import json

import pytest

from winnow.store import PLUGIN_UI_STATE_MAX_BYTES, Store

KEY = "tab:first-last.firstlast"


def test_nothing_saved_reads_as_none(store):
    assert store.get_plugin_ui_state(KEY) is None


def test_state_survives_closing_and_reopening_the_case(case_path):
    """The whole point: it is in the case file, not beside it."""
    st = Store(case_path)
    rec = st.set_plugin_ui_state(KEY, {"v": 1, "sheets": [{"name": "Logons by host"}]})
    assert rec["saved_at"]
    st.close()

    st2 = Store(case_path)
    try:
        back = st2.get_plugin_ui_state(KEY)
        assert back["payload"]["sheets"][0]["name"] == "Logons by host"
        assert back["saved_at"] == rec["saved_at"]
    finally:
        st2.close()


def test_a_second_case_does_not_see_the_first_ones_state(tmp_path):
    """Source ids and column names are per case; a spec naming one case's
    table must never be restored over another's."""
    a, b = Store(str(tmp_path / "a.db")), Store(str(tmp_path / "b.db"))
    try:
        a.set_plugin_ui_state(KEY, {"v": 1, "sheets": ["a"]})
        assert b.get_plugin_ui_state(KEY) is None
    finally:
        a.close()
        b.close()


def test_a_tab_and_a_panel_of_one_plugin_keep_their_own(store):
    """mountKey's three kinds share the <plugin>.<id> namespace, so the
    kind is part of the key or one mount overwrites the other's."""
    store.set_plugin_ui_state("tab:llm.copilot", {"v": 1, "where": "tab"})
    store.set_plugin_ui_state("panel:llm.copilot", {"v": 1, "where": "panel"})
    store.set_plugin_ui_state("page:llm.copilot", {"v": 1, "where": "page"})
    assert store.get_plugin_ui_state("tab:llm.copilot")["payload"]["where"] == "tab"
    assert store.get_plugin_ui_state("panel:llm.copilot")["payload"]["where"] == "panel"
    assert store.get_plugin_ui_state("page:llm.copilot")["payload"]["where"] == "page"


def test_writing_again_replaces_rather_than_accumulates(store):
    store.set_plugin_ui_state(KEY, {"v": 1, "sheets": [1, 2, 3]})
    store.set_plugin_ui_state(KEY, {"v": 1, "sheets": [4]})
    assert store.get_plugin_ui_state(KEY)["payload"]["sheets"] == [4]
    with store.lock:
        assert store.db.execute("SELECT COUNT(*) FROM plugin_ui_state").fetchone()[0] == 1


def test_clear_removes_it_and_clearing_nothing_is_fine(store):
    store.set_plugin_ui_state(KEY, {"v": 1})
    store.clear_plugin_ui_state(KEY)
    assert store.get_plugin_ui_state(KEY) is None
    store.clear_plugin_ui_state(KEY)


def test_a_payload_over_the_cap_is_refused_and_nothing_is_written(store):
    """The cap is what keeps a plugin from parking a result set in the case
    file, where it would go stale against the evidence it describes."""
    store.set_plugin_ui_state(KEY, {"v": 1, "sheets": ["spec"]})
    rows = [{"Host": "WKSTN-014", "User": "jsmith"}] * 4000
    with pytest.raises(ValueError) as e:
        store.set_plugin_ui_state(KEY, {"v": 1, "rows": rows})
    assert str(PLUGIN_UI_STATE_MAX_BYTES) in str(e.value)
    assert store.get_plugin_ui_state(KEY)["payload"]["sheets"] == ["spec"]


def test_an_unreadable_payload_reads_as_nothing_saved(store):
    """A hand-edited or truncated case file should start the plugin fresh,
    not raise into a mount that then shows nothing at all."""
    with store.lock, store.db:
        store.db.execute(
            "INSERT INTO plugin_ui_state(mount_key, payload, saved_at) VALUES (?,?,?)",
            (KEY, "{not json", "2026-09-22T10:00:00"))
    assert store.get_plugin_ui_state(KEY) is None


# ------------------------------------------------------------------ routes

def test_the_routes_round_trip_a_payload(client):
    assert client.get("/api/plugin_state", params={"key": KEY}).json() == {
        "mount_key": KEY, "payload": None, "saved_at": None}

    spec = {"v": 1, "active": 1, "sheets": [{"name": "A"}, {"name": "B"}]}
    w = client.post("/api/plugin_state", json={"key": KEY, "payload": spec})
    assert w.status_code == 200 and w.json()["saved_at"]

    r = client.get("/api/plugin_state", params={"key": KEY}).json()
    assert r["payload"] == spec and r["saved_at"] == w.json()["saved_at"]


def test_a_null_payload_clears(client):
    client.post("/api/plugin_state", json={"key": KEY, "payload": {"v": 1}})
    assert client.post("/api/plugin_state", json={"key": KEY, "payload": None}).json()["payload"] is None
    assert client.get("/api/plugin_state", params={"key": KEY}).json()["payload"] is None


def test_a_key_that_is_not_a_mount_key_is_a_400(client):
    """The key names a mount ('tab:'/'panel:'/'page:' plus the mount id).
    Anything else is a caller getting the contract wrong, and saying so is
    cheaper than a table of rows nothing will ever read."""
    # The newline is the one that looks fine: a key ending in one would
    # otherwise validate and then be a SECOND row, under a key no mount
    # ever asks for.
    for bad in ["", "firstlast", "sql_tabs", "widget:x.y", "tab:" + "x" * 400,
                "tab:first-last.firstlast\n", "tab:a\nb"]:
        assert client.get("/api/plugin_state", params={"key": bad}).status_code == 400, bad
        assert client.post("/api/plugin_state",
                           json={"key": bad, "payload": {"v": 1}}).status_code == 400, bad


def test_the_key_survives_a_plugin_display_name_with_punctuation(client):
    """A mount id carries the plugin's DISPLAY name, which is whatever the
    author typed — the reason the key is a parameter and not a path
    segment. 'First/Last' in a path is two segments by the time uvicorn has
    unquoted it."""
    key = "tab:First/Last.firstlast"
    client.post("/api/plugin_state", json={"key": key, "payload": {"v": 1, "ok": True}})
    assert client.get("/api/plugin_state", params={"key": key}).json()["payload"]["ok"] is True


def test_over_the_cap_is_a_400_with_the_number_in_it(client):
    big = {"v": 1, "rows": ["x" * 64] * 2000}
    r = client.post("/api/plugin_state", json={"key": KEY, "payload": big})
    assert r.status_code == 400
    assert str(PLUGIN_UI_STATE_MAX_BYTES) in json.dumps(r.json())


def test_toggling_the_plugin_off_keeps_its_state(client):
    """An analyst who turns First/Last off for a week has not thrown their
    sheets away — a toggle reloads the registry and drops every mount, and
    nothing in that path touches the case file."""
    client.post("/api/plugin_state", json={"key": KEY, "payload": {"v": 1, "sheets": [{"name": "A"}]}})
    saved = lambda: client.get("/api/plugin_state", params={"key": KEY}).json()["payload"]
    try:
        off = client.post("/api/plugins/toggle", json={"fs_name": "first_last", "scope": "off_all"})
        assert off.status_code == 200, off.text
        assert saved()["sheets"][0]["name"] == "A"
        on = client.post("/api/plugins/toggle", json={"fs_name": "first_last", "scope": "on_all"})
        assert on.status_code == 200, on.text
        assert saved()["sheets"][0]["name"] == "A"
    finally:
        # A toggle reloads the process-wide registry; put the bundled
        # example back to its shipped default for whatever runs next.
        client.post("/api/plugins/toggle", json={"fs_name": "first_last", "scope": "off_all"})


def test_a_quicklook_holding_only_tab_state_is_still_swept(tmp_path, monkeypatch):
    """The janitor keeps a quick-look forever if it holds anything an
    analyst made — including a PLUGIN's own table, where an LLM transcript
    lives. Saved tab state is the other kind: zones and sub-tabs, the shape
    of a question rather than a finding, so it sits out of _WORK_TABLES
    beside `layouts` and `case_settings`. A case someone opened a plugin
    tab in and abandoned is still abandoned."""
    import os
    import time as _time

    import server

    qdir = tmp_path / "cases" / server.QUICKLOOK_DIRNAME
    qdir.mkdir(parents=True)
    monkeypatch.setattr(server, "_cases_dir", lambda: str(tmp_path / "cases"))
    old = _time.time() - 30 * 86400

    st = Store(str(qdir / "tabstate.db"))
    try:
        st.set_plugin_ui_state(KEY, {"v": 1, "sheets": [{"name": "Bookends 1"}]})
    finally:
        st.close()
    os.utime(qdir / "tabstate.db", (old, old))

    assert server._sweep_quicklook() == 1
    assert not (qdir / "tabstate.db").exists()
