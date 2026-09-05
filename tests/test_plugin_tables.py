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
    assert t.execute("DELETE FROM {table} WHERE at < ?", ["2"]) == 1
    assert [r["content"] for r in t.rows()] == ["b"]
    assert t.execute("CREATE INDEX IF NOT EXISTS ix_chat_at ON {table} (at)") == 0  # DDL


def test_two_plugins_cannot_see_or_name_each_others_tables(store):
    a = _req(store, "llm_harness").table("chat").create(COLS)
    b = _req(store, "other_plugin").table("chat").create(COLS)
    a.insert({"role": "user", "content": "mine", "at": "1"})
    assert a.table != b.table
    assert b.rows() == []
    assert store.plugin_tables("llm_harness") == ["chat"]
    assert store.plugin_tables("other_plugin") == ["chat"]
    assert a.execute("DELETE FROM {table} WHERE 1=1") == 1
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
    assert t.table == "plugin:llm_harness:src_1"
    t.execute("DELETE FROM {table}")                  # must not touch the source
    assert store.list_sources()[0]["row_count"] == 1


def test_table_names_are_case_insensitive(store):
    t = _req(store).table("Chat").create(COLS)
    assert t.table == "plugin:llm_harness:chat"
    t.insert({"role": "user", "content": "x", "at": "t"})
    assert len(_req(store).table("chat").rows()) == 1


def test_an_odd_plugin_name_is_an_identifier_not_a_path(store):
    """fs_name is only ever a quoted SQL identifier here, so a name that
    would be alarming in a filename is merely a table called that. It is
    refused only when it would break the naming scheme."""
    t = _req(store, "../../etc").table("chat").create("x TEXT")
    assert t.table == 'plugin:../../etc:chat'
    t.insert({"x": "1"})
    assert len(t.rows()) == 1


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


# --------------------------------------------------- what the reviews found

@pytest.mark.parametrize("a_plugin,a_table,b_plugin,b_table", [
    ("llm_harness", "chat", "llm", "harness_chat"),
    ("mft_usn", "cache", "mft", "usn_cache"),          # both are real plugin names here
    ("first_last", "runs", "first", "last_runs"),
])
def test_underscored_names_cannot_collide(store, a_plugin, a_table, b_plugin, b_table):
    """`plugin_<fs>_<name>` was ambiguous whenever either half held an
    underscore — which is the house style for plugin folders — so one
    plugin could read and drop another's table by accident."""
    a = _req(store, a_plugin).table(a_table).create("id INTEGER PRIMARY KEY, content TEXT")
    a.insert({"content": "private"})
    b = _req(store, b_plugin).table(b_table).create("id INTEGER PRIMARY KEY, content TEXT")
    assert a.table != b.table
    assert b.rows() == []
    b.execute("DELETE FROM {table}")
    assert [r["content"] for r in a.rows()] == ["private"]


def test_a_plugin_folder_the_installer_accepts_can_use_tables(store):
    """A `chat-gpt` plugin loads and serves routes fine; req.table() must
    not then hand it a permanent 400 it can only fix by renaming."""
    t = _req(store, "chat-gpt").table("chat").create("x TEXT")
    assert t.table == "plugin:chat-gpt:chat"
    t.insert({"x": "1"})
    assert len(t.rows()) == 1
    assert store.plugin_tables("chat-gpt") == ["chat"]


@pytest.mark.parametrize("bad_plugin", ["", "has:colon", 'has"quote', "x" * 65])
def test_only_names_that_break_the_scheme_are_refused(store, bad_plugin):
    with pytest.raises(ValueError):
        _req(store, bad_plugin).table("chat")


def test_the_placeholder_is_not_substituted_inside_a_string_literal(store):
    """`SET meta = '{table}'` has to store the braces, not the table name."""
    t = _req(store).table("meta").create("id INTEGER PRIMARY KEY, meta TEXT")
    t.insert({"meta": "before"})
    t.execute("UPDATE {table} SET meta = '{table}'")
    assert t.rows()[0]["meta"] == "{table}"


def test_insert_accepts_the_same_columns_in_any_order(store):
    t = _req(store).table("chat").create(COLS)
    assert t.insert([{"role": "user", "content": "a", "at": "1"},
                     {"at": "2", "content": "b", "role": "assistant"}]) == 2
    assert [r["content"] for r in t.rows("ORDER BY id")] == ["a", "b"]


def test_a_limit_in_the_tail_is_honoured_and_limit_none_means_everything(store):
    t = _req(store).table("chat").create(COLS)
    t.insert([{"role": "user", "content": str(i), "at": str(i)} for i in range(20)])
    assert len(t.rows("ORDER BY id DESC LIMIT 3")) == 3      # used to be an OperationalError
    assert len(t.rows(limit=None)) == 20
    assert len(t.rows(limit=5)) == 5
    # a LIMIT inside a literal is not a LIMIT clause
    assert len(t.rows("WHERE content <> 'limit 1' ", limit=4)) == 4


def test_ddl_reports_no_rows_changed(store):
    t = _req(store).table("chat").create(COLS)
    assert t.execute("CREATE INDEX IF NOT EXISTS ix_at ON {table} (at)") == 0
    t.insert({"role": "user", "content": "x", "at": "t"})
    assert t.execute("UPDATE {table} SET content = ?", ["y"]) == 1


def test_create_on_an_existing_table_does_not_take_the_writer_lock(store):
    """The documented every-request pattern must not queue behind an import
    or compact()'s VACUUM."""
    _req(store).table("chat").create(COLS).insert({"role": "user", "content": "x", "at": "t"})
    with store.lock:
        t = _req(store).table("chat").create(COLS)     # would deadlock if it took the lock
        assert len(t.rows()) == 1


def test_isolation_is_by_naming_not_by_sandbox(store):
    """Stated plainly because the docs used to claim more: a plugin holds
    req.store and can name any table. The guarantee is no ACCIDENTAL
    collision, and that is what the tests above pin."""
    victim = _req(store, "victim").table("secrets").create("x TEXT")
    victim.insert({"x": "s3cret"})
    other = _req(store, "other").table("own").create("x TEXT")
    assert other.execute(f'DELETE FROM "{victim.table}"') == 1
    assert victim.rows() == []


def test_plugin_storage_survives_overlapping_updates(tmp_path, monkeypatch):
    """Handlers run in a threadpool now, so two of a plugin's own requests
    can overlap. get() then set() loses one side's change; update() is the
    read-modify-write that doesn't."""
    import threading
    from winnow import workspace as WS
    monkeypatch.setattr(WS, "WORKSPACE_DIR", tmp_path / "ws")
    data = WS.PluginData("racy")
    data.set({"turns": []})

    def naive(i):
        d = data.get()
        d["turns"] = d["turns"] + [i]
        data.set(d)

    def atomic(i):
        data.update(lambda d: d.__setitem__("turns", d.get("turns", []) + [i]))

    for fn, expect_all in ((naive, False), (atomic, True)):
        data.set({"turns": []})
        threads = [threading.Thread(target=fn, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = len(data.get()["turns"])
        if expect_all:
            assert got == 12, f"update() lost {12 - got} of 12 concurrent writes"
