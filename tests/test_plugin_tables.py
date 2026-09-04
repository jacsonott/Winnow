"""Plugin-owned tables in the case file: the namespacing that keeps two
plugins apart, the reader/writer split, and the fact that they are not
sources. The case that prompted them is an LLM plugin keeping its chat
transcript so it renders with the service unreachable — data that belongs
to the case and should travel with the .db, which neither req.storage
(machine-level JSON) nor ingest_rows (a browsable source) covers."""

from __future__ import annotations

import textwrap

import pytest

from winnow import plugin_api
from winnow.store import Store

COLS = "id INTEGER PRIMARY KEY, role TEXT, content TEXT, at TEXT"


def _req(store, plugin="llm_harness"):
    return plugin_api.PluginRequest("POST", "chat", {}, None, store, storage={}, plugin=plugin)


def test_a_transcript_survives_closing_and_reopening_the_case(case_path):
    """The whole point: it is in the case file, not beside it."""
    st = Store(case_path)
    t = _req(st).table("chat").create(COLS)
    t.insert([{"role": "user", "content": "what is 4624?", "at": "2026-09-04T10:00:00"},
              {"role": "assistant", "content": "A successful logon.", "at": "2026-09-04T10:00:03"}])
    st.close()

    st2 = Store(case_path)
    try:
        back = _req(st2).table("chat").rows("ORDER BY id")
        assert [r["role"] for r in back] == ["user", "assistant"]
        assert back[0]["content"] == "what is 4624?"
    finally:
        st2.close()


def test_create_is_idempotent_and_keeps_the_rows(store):
    t = _req(store).table("chat").create(COLS)
    t.insert({"role": "user", "content": "hi", "at": "t"})
    t.create(COLS)                      # every request calls this
    assert len(t.rows()) == 1


def test_rows_filters_orders_and_limits(store):
    t = _req(store).table("chat").create(COLS)
    t.insert([{"role": "user", "content": f"q{i}", "at": f"t{i}"} for i in range(5)])
    assert len(t.rows(limit=2)) == 2
    assert [r["content"] for r in t.rows("ORDER BY id DESC", limit=2)] == ["q4", "q3"]
    assert t.rows("WHERE content = ?", ["q3"])[0]["at"] == "t3"


def test_execute_takes_arbitrary_statements_against_this_table(store):
    t = _req(store).table("chat").create(COLS)
    t.insert([{"role": "user", "content": "a", "at": "1"},
              {"role": "assistant", "content": "b", "at": "2"}])
    assert t.execute("DELETE FROM {} WHERE at < ?", ["2"]) == 1
    assert [r["content"] for r in t.rows()] == ["b"]
    t.execute("CREATE INDEX IF NOT EXISTS ix_chat_at ON {} (at)")


def test_two_plugins_cannot_see_or_name_each_others_tables(store):
    a = _req(store, "llm_harness").table("chat").create(COLS)
    b = _req(store, "other_plugin").table("chat").create(COLS)
    a.insert({"role": "user", "content": "mine", "at": "1"})
    assert a.table != b.table
    assert b.rows() == []
    assert store.plugin_tables("llm_harness") == ["chat"]
    assert store.plugin_tables("other_plugin") == ["chat"]
    # A plugin's SQL only ever names its own table, through {}.
    assert a.execute("DELETE FROM {} WHERE 1=1") == 1
    assert b.rows() == []


@pytest.mark.parametrize("bad", ["../evil", "a b", "", "9lives", "x" * 42, "a-b", "a.b",
                                 'x"; DROP TABLE sources; --'])
def test_bad_table_names_are_refused(store, bad):
    with pytest.raises(ValueError):
        _req(store).table(bad)


def test_a_name_that_looks_like_an_internal_table_is_still_only_its_own(store, write_csv):
    """`src_1` is a fine plugin table name because it is namespaced — what
    would be dangerous is it addressing the real source table, and it
    cannot."""
    store.ingest_csv(write_csv([["A"], ["1"]], "real.csv"), name="real.csv", build_fts=False)
    t = _req(store).table("src_1").create("x TEXT")
    assert t.table == "plugin_llm_harness_src_1"
    t.execute("DELETE FROM {}")                       # must not touch the source
    assert store.list_sources()[0]["row_count"] == 1


def test_table_names_are_case_insensitive(store):
    t = _req(store).table("Chat").create(COLS)
    assert t.table == "plugin_llm_harness_chat"
    t.insert({"role": "user", "content": "x", "at": "t"})
    assert len(_req(store).table("chat").rows()) == 1


def test_a_bad_plugin_name_is_refused_too(store):
    with pytest.raises(ValueError):
        _req(store, "../../etc").table("chat")


def test_a_plugin_table_is_not_a_source(store, write_csv):
    """It must not appear in the grid, the sidebar or a merge — those all
    read the `sources` table, which this never touches."""
    store.ingest_csv(write_csv([["A"], ["1"]], "real.csv"), name="real.csv", build_fts=False)
    _req(store).table("chat").create(COLS).insert({"role": "user", "content": "x", "at": "t"})
    assert [s["name"] for s in store.list_sources()] == ["real.csv"]


def test_no_case_open_is_a_clear_error():
    req = plugin_api.PluginRequest("GET", "chat", {}, None, None, storage={}, plugin="llm_harness")
    with pytest.raises(ValueError, match="No case is open"):
        req.table("chat")


def test_a_request_without_a_plugin_identity_cannot_make_tables(store):
    req = plugin_api.PluginRequest("GET", "chat", {}, None, store, storage={})
    with pytest.raises(ValueError, match="plugin identity"):
        req.table("chat")


def test_reads_do_not_take_the_writer_lock(store):
    """Invariant #4: reading a transcript while an import holds the writer
    lock must not queue behind it. A deadlock here, not a race."""
    t = _req(store).table("chat").create(COLS)
    t.insert({"role": "user", "content": "hi", "at": "t"})
    with store.lock:
        assert len(t.rows()) == 1
        assert store.plugin_tables("llm_harness") == ["chat"]
        assert t.exists() is True


def test_drop_removes_it(store):
    t = _req(store).table("chat").create(COLS)
    assert t.exists()
    t.drop()
    assert not t.exists() and store.plugin_tables("llm_harness") == []


def test_insert_rejects_ragged_rows(store):
    t = _req(store).table("chat").create(COLS)
    with pytest.raises(ValueError, match="same columns"):
        t.insert([{"role": "user"}, {"content": "x"}])
    assert t.insert([]) == 0


def test_a_plugin_route_keeps_its_transcript(client, store, tmp_path, monkeypatch):
    """End to end through the HTTP dispatcher, the way the LLM plugin will
    use it: POST a turn, read the history back on a later request."""
    import server
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "llm_harness.py").write_text(textwrap.dedent('''
        PLUGIN = {"name": "llm_harness", "version": "0.1", "description": "chat with history"}

        COLS = "id INTEGER PRIMARY KEY, role TEXT, content TEXT"

        def _chat(req):
            t = req.table("chat").create(COLS)
            if req.method == "POST":
                t.insert({"role": req.body["role"], "content": req.body["content"]})
            return {"messages": t.rows("ORDER BY id")}

        def register(api):
            api.register_api("chat", _chat, methods=("GET", "POST"))
    '''))
    reg = plugin_api.PluginRegistry()
    reg.load([pdir])
    monkeypatch.setattr(server, "PLUGINS", reg)

    r = client.post("/api/plugin/llm_harness/chat", json={"role": "user", "content": "what is 4624?"})
    assert r.status_code == 200, r.text
    assert [m["content"] for m in r.json()["messages"]] == ["what is 4624?"]
    client.post("/api/plugin/llm_harness/chat", json={"role": "assistant", "content": "A logon."})
    # A later GET renders the history with no remote service involved.
    got = client.get("/api/plugin/llm_harness/chat").json()["messages"]
    assert [m["role"] for m in got] == ["user", "assistant"]
    assert store.plugin_tables("llm_harness") == ["chat"]
