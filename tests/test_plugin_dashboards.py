"""Dashboards a plugin offers (register_dashboard).

A plugin could already build a board — `req.store` is the whole Store, so
`create_dashboard` was one call away — but nothing said so, nothing
described the widget shape, and nothing stopped the capability
disappearing in a refactor. This is that surface made explicit.

The rule the tests are really about: a board is OFFERED, never applied.
Loading a plugin must not put anything in anyone's case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from winnow.plugin_api import PluginRegistry

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"

GOOD = [{"title": "Rows", "source": "sql", "render": "stat",
         "query": {"sql": "SELECT COUNT(*) FROM {{evtx}}"}}]


def _plugin(tmp_path, body: str, name: str = "boards") -> PluginRegistry:
    d = tmp_path / name
    d.mkdir()
    (d / "__init__.py").write_text(body)
    reg = PluginRegistry()
    reg.load([tmp_path])
    return reg


def _register(widgets, **kw) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in kw.items())
    return (f"def register(api):\n"
            f"    api.register_dashboard(id='b1', label='Board one', "
            f"widgets={widgets!r}{', ' + args if args else ''})\n")


def test_a_registered_board_is_listed_without_its_widgets(tmp_path):
    reg = _plugin(tmp_path, _register(GOOD))
    (b,) = reg.list_dashboards()
    assert b["label"] == "Board one" and b["widget_count"] == 1
    assert b["plugin_fs"] == "boards" and b["local_id"] == "b1"
    assert "widgets" not in b, "the list is for naming boards, not shipping them"


def test_the_widgets_are_fetched_by_id(tmp_path):
    reg = _plugin(tmp_path, _register(GOOD))
    assert reg.get_dashboard("boards", "b1")["widgets"] == GOOD
    assert reg.get_dashboard("boards", "nope") is None
    assert reg.get_dashboard("other", "b1") is None


def test_a_broken_plugin_is_recorded_not_raised(tmp_path):
    """A bad board must not take the plugin — or the server — down."""
    reg = _plugin(tmp_path, _register([{"source": "sql"}]))   # no title
    (p,) = reg.plugins
    assert p["error"] and "title" in p["error"]
    assert reg.list_dashboards() == []


@pytest.mark.parametrize("widgets,msg", [
    ([], "non-empty"),
    ([{"source": "sql", "render": "stat", "query": {"sql": "SELECT 1"}}], "title"),
    ([{"title": "x", "render": "stat"}], "source"),
    ([{"title": "x", "source": "watchlst", "render": "stat"}], "source"),
    ([{"title": "x", "source": "sql", "query": {"sql": "SELECT 1"}}], "render"),
    ([{"title": "x", "source": "sql", "render": "table", "query": {"sql": "SELECT 1"}}], "render"),
    ([{"title": "x", "source": "sql", "render": "stat"}], "query.sql"),
    ([{"title": "x", "source": "sql", "render": "stat", "query": {}}], "query.sql"),
])
def test_a_widget_that_could_not_render_is_refused_at_registration(tmp_path, widgets, msg):
    """Better here, naming the widget, than as a card reading "no such
    column" in a case six weeks later."""
    reg = _plugin(tmp_path, _register(widgets))
    (p,) = reg.plugins
    assert p["error"] and msg in p["error"], p["error"]


def test_two_boards_cannot_collide(tmp_path):
    body = ("def register(api):\n"
            f"    api.register_dashboard(id='b1', label='One', widgets={GOOD!r})\n"
            f"    api.register_dashboard(id='b1', label='Two', widgets={GOOD!r})\n")
    reg = _plugin(tmp_path, body)
    assert "Duplicate dashboard id" in (reg.plugins[0]["error"] or "")


def test_an_id_that_is_not_a_slug_is_refused(tmp_path):
    reg = _plugin(tmp_path, _register(GOOD).replace("id='b1'", "id='Not A Slug'"))
    assert "lowercase" in (reg.plugins[0]["error"] or "")


# ------------------------------------------------------------ the routes

def test_the_listing_carries_offered_boards(client, monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "PLUGINS", _plugin(tmp_path, _register(GOOD)))
    # The listing rides /api/plugins, the way tabs, panels and row actions
    # do — there is no second route for it to drift from.
    boards = client.get("/api/plugins").json()["dashboards"]
    assert [b["label"] for b in boards] == ["Board one"]
    assert boards[0]["widget_count"] == 1 and "widgets" not in boards[0]


def test_adding_one_copies_it_into_the_open_case(client, store, write_csv, monkeypatch, tmp_path):
    import server

    store.ingest_csv(write_csv([["a"], ["1"]], "e.csv"), name="e", build_fts=False)
    widgets = [{"title": "Rows", "source": "sql", "render": "stat",
                "query": {"sql": "SELECT COUNT(*) FROM src_1"}}]
    monkeypatch.setattr(server, "PLUGINS", _plugin(tmp_path, _register(widgets)))

    assert store.list_dashboards() == [], "loading a plugin must not create anything"
    r = client.post("/api/plugin_dashboards/boards/b1/add", json={})
    assert r.status_code == 200, r.text
    assert [b["name"] for b in store.list_dashboards()] == ["Board one"]
    assert store.get_dashboard(r.json()["id"]) == widgets


def test_adding_twice_refreshes_rather_than_duplicates(client, store, write_csv, monkeypatch, tmp_path):
    """Both calls must SUCCEED. Asserting only the board count let this pass
    while the second add was refused with a 409 — one board, for the wrong
    reason, and the refresh the guide promises never happened."""
    import server

    store.ingest_csv(write_csv([["a"], ["1"]], "e.csv"), name="e", build_fts=False)
    monkeypatch.setattr(server, "PLUGINS", _plugin(tmp_path, _register(GOOD)))
    first = client.post("/api/plugin_dashboards/boards/b1/add", json={})
    assert first.status_code == 200, first.text

    # The plugin ships a new version of the same board; adding it again is
    # how an analyst picks that up, so it must not ask about a collision
    # with the copy it wrote itself a moment ago.
    updated = [{"title": "Rows (v2)", "source": "sql", "render": "stat",
                "query": {"sql": "SELECT COUNT(*) FROM {{evtx}}"}}]
    v2 = tmp_path / "v2"
    v2.mkdir()
    monkeypatch.setattr(server, "PLUGINS", _plugin(v2, _register(updated)))
    second = client.post("/api/plugin_dashboards/boards/b1/add", json={})
    assert second.status_code == 200, second.text

    assert len(store.list_dashboards()) == 1
    assert second.json()["id"] == first.json()["id"]
    assert store.get_dashboard(first.json()["id"])[0]["title"] == "Rows (v2)", \
        "the second add returned 200 without refreshing the widgets"


def test_another_plugins_board_of_the_same_name_still_asks(client, store, write_csv, monkeypatch, tmp_path):
    """`origin` says whose copy a board is, not merely that some plugin made
    it — so a second plugin claiming the same label is still a question."""
    import server

    store.ingest_csv(write_csv([["a"], ["1"]], "e.csv"), name="e", build_fts=False)
    monkeypatch.setattr(server, "PLUGINS", _plugin(tmp_path, _register(GOOD)))
    first = client.post("/api/plugin_dashboards/boards/b1/add", json={})
    assert first.status_code == 200, first.text

    monkeypatch.setattr(server, "PLUGINS",
                        _plugin(tmp_path, _register(GOOD), name="others"))
    r = client.post("/api/plugin_dashboards/others/b1/add", json={})
    assert r.status_code == 409
    assert r.json()["detail"]["dashboard_id"] == first.json()["id"]


def test_editing_the_board_by_hand_makes_it_the_analysts(client, store, write_csv, monkeypatch, tmp_path):
    """The silent refresh lasts exactly as long as the widgets are still the
    plugin's. Change one card and there IS work of the analyst's to lose,
    so the next add asks like any other collision."""
    import server

    store.ingest_csv(write_csv([["a"], ["1"]], "e.csv"), name="e", build_fts=False)
    monkeypatch.setattr(server, "PLUGINS", _plugin(tmp_path, _register(GOOD)))
    first = client.post("/api/plugin_dashboards/boards/b1/add", json={})
    assert first.status_code == 200, first.text

    store.set_dashboard_widgets(
        first.json()["id"], [{"title": "Mine", "source": "tags", "render": "stat"}])

    r = client.post("/api/plugin_dashboards/boards/b1/add", json={})
    assert r.status_code == 409
    assert store.get_dashboard(first.json()["id"])[0]["title"] == "Mine", \
        "it refreshed over the analyst's edit"


def test_a_profile_writing_the_board_takes_it_back_from_the_plugin(client, store, write_csv, monkeypatch, tmp_path):
    """A profile applies ITS widgets under that name, so the board stops
    being the plugin's copy — the same rule as a hand edit, reached the way
    `upsert_dashboard_by_name` is reached from a profile apply."""
    import server

    store.ingest_csv(write_csv([["a"], ["1"]], "e.csv"), name="e", build_fts=False)
    monkeypatch.setattr(server, "PLUGINS", _plugin(tmp_path, _register(GOOD)))
    first = client.post("/api/plugin_dashboards/boards/b1/add", json={})
    assert first.status_code == 200, first.text

    store.upsert_dashboard_by_name(
        "Board one", [{"title": "From a profile", "source": "tags", "render": "stat"}])

    r = client.post("/api/plugin_dashboards/boards/b1/add", json={})
    assert r.status_code == 409
    assert store.get_dashboard(first.json()["id"])[0]["title"] == "From a profile"


def test_an_unknown_board_is_a_404(client, monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "PLUGINS", _plugin(tmp_path, _register(GOOD)))
    assert client.post("/api/plugin_dashboards/boards/nope/add", json={}).status_code == 404


def test_it_will_not_overwrite_a_board_the_analyst_built(client, store, write_csv, monkeypatch, tmp_path):
    """The name comes from the PLUGIN, so a board the analyst made can
    share it by coincidence. Replacing it would discard their widgets with
    no undo — so a taken name is a question, not a default."""
    import server

    store.ingest_csv(write_csv([["a"], ["1"]], "e.csv"), name="e", build_fts=False)
    monkeypatch.setattr(server, "PLUGINS", _plugin(tmp_path, _register(GOOD)))
    mine = store.create_dashboard("Board one", [{"title": "Mine", "source": "tags", "render": "stat"}])

    r = client.post("/api/plugin_dashboards/boards/b1/add", json={})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "name_taken" and detail["dashboard_id"] == mine["id"]
    assert store.get_dashboard(mine["id"])[0]["title"] == "Mine", "it replaced them anyway"

    # A different name lands beside it, untouched.
    assert client.post("/api/plugin_dashboards/boards/b1/add",
                       json={"name": "Board one (plugin)"}).status_code == 200
    assert store.get_dashboard(mine["id"])[0]["title"] == "Mine"
    # …and replace=true is the answer to the question it asked.
    r = client.post("/api/plugin_dashboards/boards/b1/add", json={"replace": True})
    assert r.status_code == 200
    assert store.get_dashboard(mine["id"])[0]["title"] == "Rows"


# --------------------------------------------------- the shipped example

def test_the_esxi_example_offers_a_board_that_holds_together():
    """The reference implementation. Its widgets must be the shape the
    hook documents, and its drills must name columns the header set has."""
    from winnow import defaults

    reg = PluginRegistry()
    reg.load([EXAMPLES])
    board = reg.get_dashboard("esxi_logs", "overview")
    assert board, [p["name"] for p in reg.plugins]
    cols = set(dict(defaults.headers()["nicknames"])["ESXi / Linux host logs"])
    for w in board["widgets"]:
        assert w["title"] and w["source"] == "sql" and w["query"]["sql"]
        drill = w.get("drill")
        assert drill, f"{w['title']} is not clickable"
        named = [c["column"] for c in drill.get("where", [])]
        if drill.get("column"):
            named.append(drill["column"])
        for node in (drill.get("tree", {}).get("children") or []):
            named += [c["column"] for c in (node.get("children") or [])] or [node.get("column")]
        assert all(c in cols for c in named if c), (w["title"], named)


LOGS = {
    "auth.log": ("2026-03-14T08:00:00Z h sshd[1]: Failed password for root from 10.0.0.9 port 2\n"
                 "2026-03-14T08:01:00Z h sshd[2]: Invalid user admin from 10.0.0.9 port 3\n"
                 "2026-03-14T08:05:00Z h sshd[3]: Accepted password for adm from 10.0.0.8 port 4\n"),
    "shell.log": "2026-03-14T09:00:00Z h shell[4]: esxcli network firewall get\n",
}


@pytest.fixture
def esxi_case(store, tmp_path):
    """A case holding the logs the example board is written for, ingested
    through the plugin's own format — two tables, which is the point: the
    board's queries span them with {{all:header_set:…}}."""
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    fmt = reg.get_format("esxi-logs.esxi_log")
    for name, text in LOGS.items():
        p = tmp_path / name
        p.write_text(text)
        parsed = fmt.parse(str(p), fmt.resolve_options({}))
        store.ingest_rows(parsed["columns"], parsed["rows"], name=name,
                          column_types=parsed["column_types"], build_fts=False)
    return store, reg.get_dashboard("esxi_logs", "overview")


def test_every_widget_on_the_example_board_renders(esxi_case):
    store, board = esxi_case
    got = {w["title"]: store.dashboard_widget_preview(w["source"], w["query"])["rows"]
           for w in board["widgets"]}
    assert got["Log lines"] == [[4]]
    assert got["Log types"] == [["auth", 3], ["shell", 1]]     # spans both tables
    assert got["Failed SSH logins"] == [[1 + 1]]               # Failed + Invalid user
    assert sorted(got["Top source IPs"]) == [["10.0.0.8", 1], ["10.0.0.9", 2]]
    assert got["Activity over time"] == [["2026-03-14 08", 3], ["2026-03-14 09", 1]]


def test_every_drill_opens_exactly_what_its_widget_counted(esxi_case):
    """The mistake easiest to ship, and the one the guide warns about: a
    number whose click contradicts it."""
    store, board = esxi_case
    sids = [s["id"] for s in store.list_sources() if not s.get("is_merge")]

    def opened(drill, value=None):
        kids = [{"type": "cond", **c} for c in drill.get("where", [])]
        if drill.get("tree"):
            kids.append(drill["tree"])
        if value is not None and drill.get("column"):
            kids.append({"type": "cond", "column": drill["column"], "op": "equals", "value": value})
        tree = {"type": "group", "op": "AND", "children": kids}
        return sum(len(store.run_sql(store.spec_sql(sid, {"source_id": sid, "filter_tree": tree}),
                                     limit=1000)["rows"]) for sid in sids)

    by_title = {w["title"]: w for w in board["widgets"]}
    for title in ("Log lines", "Failed SSH logins"):
        w = by_title[title]
        (n,) = store.dashboard_widget_preview(w["source"], w["query"])["rows"][0],
        assert opened(w["drill"]) == n[0], title
    for title in ("Log types", "Top source IPs"):
        w = by_title[title]
        for label, count in store.dashboard_widget_preview(w["source"], w["query"])["rows"]:
            assert opened(w["drill"], value=str(label)) == count, (title, label)
