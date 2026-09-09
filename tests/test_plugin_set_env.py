"""Plugins saving WINNOW_* environment variables: the same operation
Settings → Environment performs, so a plugin that obtained a token itself
(an OAuth exchange, a key pasted into its own tab) can persist it instead
of sending the analyst somewhere else to retype it.

Not a new privilege — a plugin is arbitrary Python and could always write
the file — so what these pin is that it cannot do what the ANALYST cannot,
and that the analyst can still see and undo it.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from winnow import plugin_api, userenv


def _req(store=None, plugin="llm_harness", loopback=True):
    return plugin_api.PluginRequest("POST", "x", {}, None, store, storage={},
                                    plugin=plugin, loopback=loopback)


def test_a_plugin_saves_a_token_and_reads_it_straight_back():
    req = _req()
    assert req.set_env("winnow_llm_key", "s3cret") == "WINNOW_LLM_KEY"   # normalised
    assert req.env("WINNOW_LLM_KEY") == "s3cret"
    assert os.environ["WINNOW_LLM_KEY"] == "s3cret"
    # …and it is in the store, so the next launch has it
    assert userenv.store().load()["WINNOW_LLM_KEY"] == "s3cret"


def test_it_shows_up_in_the_panel_the_analyst_manages(client):
    _req().set_env("WINNOW_LLM_KEY", "s3cret")
    listed = {v["name"]: v for v in client.get("/api/env").json()["vars"]}
    assert listed["WINNOW_LLM_KEY"] == {"name": "WINNOW_LLM_KEY", "stored": True, "live": True,
                                        "reserved": False, "shell": False}
    # the value is still never sent to a browser
    assert "s3cret" not in client.get("/api/env").text
    # and the analyst can take it away again
    assert client.delete("/api/env/WINNOW_LLM_KEY").json()["ok"] is True
    assert _req().env("WINNOW_LLM_KEY") is None


def test_a_plugin_can_remove_what_it_saved():
    req = _req()
    req.set_env("WINNOW_LLM_KEY", "s3cret")
    req.unset_env("WINNOW_LLM_KEY")
    assert req.env("WINNOW_LLM_KEY") is None
    assert "WINNOW_LLM_KEY" not in userenv.store().load()


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "PATH", "AWS_SECRET_ACCESS_KEY",
                                  "winnow_lower_ok_but_this_is_fine", "WINNOW_"])
def test_it_can_only_write_winnow_names(name):
    req = _req()
    if name == "winnow_lower_ok_but_this_is_fine":
        assert req.set_env(name, "v") == name.upper()      # case is normalised, not rejected
        return
    with pytest.raises(ValueError):
        req.set_env(name, "v")
    assert name not in os.environ or name == "PATH"


def test_it_cannot_rewrite_winnows_own_settings():
    """A plugin must not be able to relocate the workspace or the plugin
    directory — the panel refuses those too."""
    for knob in sorted(userenv.RESERVED):
        with pytest.raises(ValueError, match="own settings"):
            _req().set_env(knob, "/somewhere/else")


def test_a_value_exported_outside_winnow_still_wins(monkeypatch):
    monkeypatch.setenv("WINNOW_LLM_KEY", "from-the-shell")
    with pytest.raises(ValueError, match="set outside Winnow"):
        _req().set_env("WINNOW_LLM_KEY", "from-the-plugin")
    assert os.environ["WINNOW_LLM_KEY"] == "from-the-shell"
    # and it is not the plugin's to delete either
    _req().unset_env("WINNOW_LLM_KEY")
    assert os.environ["WINNOW_LLM_KEY"] == "from-the-shell"


@pytest.mark.parametrize("bad", ["two\nlines", "x" * (userenv.MAX_VALUE + 1), None])
def test_bad_values_are_refused(bad):
    with pytest.raises(ValueError):
        _req().set_env("WINNOW_LLM_KEY", bad)


def test_a_handler_can_tell_whether_its_caller_is_local():
    """Winnow's own env routes are loopback-only; a plugin route is not, so
    a plugin that does not want a remote viewer triggering a save has to be
    able to ask."""
    assert _req().is_loopback is True
    assert _req(loopback=False).is_loopback is False
    # constructed outside the HTTP path (a test, a background task): local
    assert plugin_api.PluginRequest("GET", "x", {}, None, None).is_loopback is True


def test_the_dispatcher_reports_a_remote_caller(client, tmp_path, monkeypatch):
    import server
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "wherefrom.py").write_text(textwrap.dedent('''
        PLUGIN = {"name": "wherefrom", "version": "0.1", "description": "reports its caller"}
        def register(api):
            api.register_api("who", lambda req: {"local": req.is_loopback})
    '''))
    reg = plugin_api.PluginRegistry()
    reg.load([pdir])
    monkeypatch.setattr(server, "PLUGINS", reg)
    assert client.get("/api/plugin/wherefrom/who").json() == {"local": True}
    monkeypatch.setattr(server, "_is_loopback", lambda request: False)
    assert client.get("/api/plugin/wherefrom/who").json() == {"local": False}


def test_end_to_end_through_a_plugin_route(client, tmp_path, monkeypatch):
    """What the LLM plugin will actually do: take a key from its own tab,
    save it, and report that it is configured without ever echoing it."""
    import server
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "keyholder.py").write_text(textwrap.dedent('''
        PLUGIN = {"name": "keyholder", "version": "0.1", "description": "saves its own key"}

        def _key(req):
            if req.method == "POST":
                if not req.is_loopback:
                    raise ValueError("Set the key from the machine Winnow runs on")
                req.set_env("WINNOW_KEYHOLDER_KEY", req.body["key"])
            return {"configured": bool(req.env("WINNOW_KEYHOLDER_KEY"))}

        def register(api):
            api.register_api("key", _key, methods=("GET", "POST"))
    '''))
    reg = plugin_api.PluginRegistry()
    reg.load([pdir])
    monkeypatch.setattr(server, "PLUGINS", reg)

    assert client.get("/api/plugin/keyholder/key").json() == {"configured": False}
    r = client.post("/api/plugin/keyholder/key", json={"key": "sk-abc123"})
    assert r.status_code == 200 and r.json() == {"configured": True}
    assert "sk-abc123" not in r.text
    assert os.environ["WINNOW_KEYHOLDER_KEY"] == "sk-abc123"

    # the plugin's own remote guard turns into the 400 contract
    monkeypatch.setattr(server, "_is_loopback", lambda request: False)
    assert client.post("/api/plugin/keyholder/key", json={"key": "x"}).status_code == 400
    assert os.environ["WINNOW_KEYHOLDER_KEY"] == "sk-abc123"
