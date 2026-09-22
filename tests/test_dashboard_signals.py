"""`signals` — one card, many numbers, and every cell keeps its own drill.

The render kind exists because folding eleven single-number cards into a
`kv` card folds eleven working drill-throughs into one OR-of-everything,
and a click that opens a superset of the rows you were reading is worse
than one that opens nothing. So a signals widget is `source: "cells"`
plus a list of small widgets — each with its own source, query and drill
— and `Store._cells_preview` answers with one label/value row per cell.

What is pinned here is the three properties that make it worth having: a
cell answers in its own place (including when it cannot), the card is one
request whatever the cell count, and the cache knows the difference
between editing a cell and renaming the card.
"""

from __future__ import annotations

import pytest

from winnow import defaults, plugin_api

EVTX_COLS = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]


def _ev(**kw):
    d = {c: "" for c in EVTX_COLS}
    d.update(kw)
    return [d[c] for c in EVTX_COLS]


ROWS = [
    _ev(TimeCreated="2026-03-14 08:00:00", EventId="4624", Channel="Security", UserName="jsmith"),
    _ev(TimeCreated="2026-03-14 08:01:00", EventId="4625", Channel="Security", UserName="admin"),
    _ev(TimeCreated="2026-03-14 08:02:00", EventId="4625", Channel="Security", UserName="admin"),
]


def _cell(label, sql, **kw):
    c = {"label": label, "source": "sql", "query": {"sql": sql}}
    c.update(kw)
    return c


CARD = {
    "title": "Triage signals", "source": "cells", "render": "signals", "span": 3,
    "cells": [
        _cell("Logons", "SELECT COUNT(*) FROM {{evtx}} WHERE EventId='4624'",
              drill={"table": "{{evtx}}", "where": [{"column": "EventId", "op": "equals", "value": "4624"}]}),
        _cell("Failed logons", "SELECT COUNT(*) FROM {{evtx}} WHERE EventId='4625'", tone="warn",
              drill={"table": "{{evtx}}", "where": [{"column": "EventId", "op": "equals", "value": "4625"}]}),
        {"label": "Tagged findings", "source": "tags"},
    ],
}


@pytest.fixture
def logs(store, write_csv):
    sid = store.ingest_csv(write_csv([EVTX_COLS] + ROWS, "evtx.csv"), name="evtx", build_fts=False)["id"]
    return store, sid


def _preview(store, w):
    return store.dashboard_widget_preview(w.get("source") or "sql", w.get("query") or {}, cells=w.get("cells"))


def test_a_card_answers_one_row_per_cell_in_cell_order(logs):
    store, _ = logs
    out = _preview(store, CARD)
    assert out["columns"] == ["label", "value"]
    assert out["rows"] == [["Logons", 1], ["Failed logons", 2], ["Tagged findings", 0]]
    assert out["cell_errors"] == [None, None, None]


def test_a_cell_whose_table_is_missing_fails_alone(store):
    """The failure mode the old board had: on a case with no RECmd batch,
    five host-fact cards were five identical error cards. One card with
    one query would have been one error where six numbers used to be —
    and would have taken the evtx cells down with the registry ones."""
    card = {"title": "Mixed", "source": "cells", "render": "signals", "cells": [
        _cell("Registry values", "SELECT COUNT(*) FROM {{registry}}"),
        {"label": "Tagged findings", "source": "tags"},
    ]}
    out = store.dashboard_widget_preview("cells", {}, cells=card["cells"])
    assert out["rows"] == [["Registry values", None], ["Tagged findings", 0]]
    assert "Registry (RECmd batch)" in out["cell_errors"][0]
    assert out["cell_errors"][1] is None


def test_a_cell_cannot_be_a_grid_of_cells(store):
    out = store.dashboard_widget_preview("cells", {}, cells=[{"label": "Nope", "source": "cells"}])
    assert out["rows"] == [["Nope", None]]
    assert "cannot itself be a grid" in out["cell_errors"][0]


def test_a_watchlist_cell_uses_its_total(store):
    store.add_indicator("rclone", kind="filename")
    out = store.dashboard_widget_preview("cells", {}, cells=[{"label": "Watchlist hits", "source": "watchlist"}])
    assert out["rows"] == [["Watchlist hits", 0]]


# ------------------------------------------------------------- the cache

def test_editing_a_cell_drops_the_cached_answer_and_retitling_does_not(store, logs):
    """The store fingerprints the question, not the widget. A cell's
    LABEL is part of its question, unlike a card's title, because the
    label is in the answer — the payload is label/value pairs and the
    card reads each drill back off it by label."""
    fp = store._widget_fingerprint
    base = fp(CARD)
    assert fp({**CARD, "title": "Signals", "span": 4}) == base
    edited = {**CARD, "cells": [dict(CARD["cells"][0], query={"sql": "SELECT 1"}), *CARD["cells"][1:]]}
    assert fp(edited) != base
    relabelled = {**CARD, "cells": [dict(CARD["cells"][0], label="Successful logons"), *CARD["cells"][1:]]}
    assert fp(relabelled) != base


def test_a_signals_card_caches_and_refreshes_like_any_other(client, logs):
    store, _ = logs
    did = store.create_dashboard("Signals board", [CARD])["id"]
    wid = store.get_dashboard(did)[0]["id"]

    pv = client.post("/api/dashboard/widget/preview", json={
        "source": "cells", "query": {}, "cells": CARD["cells"],
        "dashboard_id": did, "widget_id": wid})
    assert pv.status_code == 200
    assert pv.json()["rows"][1] == ["Failed logons", 2]
    assert pv.json()["ran_at"]

    cached = client.get(f"/api/dashboards/{did}").json()["cache"][wid]
    assert cached["payload"]["rows"][1] == ["Failed logons", 2] and cached["stale"] is False

    r = client.post(f"/api/dashboards/{did}/refresh", json={}).json()
    assert r["results"][wid]["payload"]["rows"][0] == ["Logons", 1]


def test_the_whole_card_is_one_request(client, logs):
    """Eleven cards were eleven round trips; eleven cells are one."""
    store, _ = logs
    did = store.create_dashboard("One request", [CARD])["id"]
    wid = store.get_dashboard(did)[0]["id"]
    out = client.post("/api/dashboard/widget/preview", json={
        "source": "cells", "query": {}, "cells": CARD["cells"],
        "dashboard_id": did, "widget_id": wid}).json()
    assert len(out["rows"]) == len(CARD["cells"])


# ------------------------------------------------------- merge parity (#9)

def test_a_signals_cell_reads_a_merged_case_the_way_every_widget_does(store, write_csv):
    """Dashboards are a standing exception in invariant #9: a merge has no
    `src_N`, `_sources_for_header_set` skips merges and a `{{evtx}}` widget
    binds to the first member. A new render kind inherits that rather than
    inventing a second answer — so a cell and a plain sql widget asking
    the same question on a merged case agree, and a drill aimed at the
    merge itself is refused in both."""
    a = store.ingest_csv(write_csv([EVTX_COLS] + ROWS[:1], "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv([EVTX_COLS] + ROWS[1:], "b.csv"), name="b", build_fts=False)["id"]
    merge = store.create_merge("both", [a, b])
    assert merge["id"] < 0

    sql = "SELECT COUNT(*) FROM {{evtx}}"
    plain = store.dashboard_widget_preview("sql", {"sql": sql})["rows"][0][0]
    cells = store.dashboard_widget_preview("cells", {}, cells=[_cell("Events", sql)])["rows"][0][1]
    assert cells == plain == 1          # the first member, not the merge's two rows

    with pytest.raises(ValueError):
        store.resolve_table_sources(f"src_{merge['id']}")


# ------------------------------------------------ what a plugin may register

def _api(tmp_path):
    return plugin_api.PluginAPI(plugin_api.PluginRegistry(), "demo", "demo", tmp_path)


def _register(api, cells):
    api.register_dashboard(id="board", label="Board", widgets=[
        {"title": "Signals", "source": "cells", "render": "signals", "cells": cells}])


def test_a_plugin_may_register_a_signals_board(tmp_path):
    api = _api(tmp_path)
    _register(api, [_cell("Logons", "SELECT 1"), {"label": "Tags", "source": "tags"}])
    board = api._registry.get_dashboard("demo", "board")
    assert board["widgets"][0]["render"] == "signals"


@pytest.mark.parametrize("cells, says", [
    ([], "no cells"),
    ([{"source": "sql", "query": {"sql": "SELECT 1"}}], "needs a label"),
    ([_cell("Same", "SELECT 1"), _cell("Same", "SELECT 2")], "two cells labelled"),
    ([{"label": "Nested", "source": "cells"}], "has source 'cells'"),
    ([{"label": "Empty", "source": "sql"}], "no query.sql"),
])
def test_a_bad_cell_is_the_plugins_load_error_not_a_blank_card(tmp_path, cells, says):
    with pytest.raises(ValueError, match=says):
        _register(_api(tmp_path), cells)
