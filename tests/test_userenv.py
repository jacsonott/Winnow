"""WINNOW_* environment variables (winnow/userenv.py): the prefix rule,
the two stores, startup loading with the shell winning, the loopback-only
routes that never return a value, and PluginRequest.env."""

from __future__ import annotations

import os
import stat

import pytest

from winnow import plugin_api, userenv


# ------------------------------------------------------------------ names

@pytest.mark.parametrize("name", ["WINNOW_TOKEN", "WINNOW_VT_API_KEY", "WINNOW_A1"])
def test_good_names(name):
    assert userenv.check_name(name, for_write=True) == name


@pytest.mark.parametrize("name", ["TOKEN", "AWS_SECRET_ACCESS_KEY", "PATH", "winnow_token", "WINNOW_",
                                  "WINNOW_bad", "WINNOW_A B", "WINNOW_" + "X" * 60, ""])
def test_bad_names(name):
    with pytest.raises(ValueError):
        userenv.check_name(name)


def test_winnows_own_knobs_are_readable_but_not_writable():
    assert userenv.check_name("WINNOW_WORKSPACE_DIR") == "WINNOW_WORKSPACE_DIR"
    with pytest.raises(ValueError, match="own settings"):
        userenv.check_name("WINNOW_WORKSPACE_DIR", for_write=True)


def test_values_are_single_line_and_capped():
    with pytest.raises(ValueError):
        userenv.check_value("a\nb")
    with pytest.raises(ValueError):
        userenv.check_value("x" * (userenv.MAX_VALUE + 1))
    with pytest.raises(ValueError):
        userenv.check_value(None)
    assert userenv.check_value("  spaced  ") == "  spaced  "


# ------------------------------------------------------------- file store

def test_file_store_round_trip_is_owner_only(tmp_path):
    st = userenv.FileEnvStore(tmp_path / "cfg" / "winnow" / "env")
    assert st.load() == {}
    st.set("WINNOW_TOKEN", "s3cret=with=equals")
    st.set("WINNOW_OTHER", "two")
    assert st.load() == {"WINNOW_TOKEN": "s3cret=with=equals", "WINNOW_OTHER": "two"}
    mode = stat.S_IMODE(st.path.stat().st_mode)
    assert mode == 0o600, oct(mode)
    assert stat.S_IMODE(st.path.parent.stat().st_mode) == 0o700
    st.delete("WINNOW_TOKEN")
    assert st.load() == {"WINNOW_OTHER": "two"}
    st.delete("WINNOW_MISSING")   # no-op
    assert not [p for p in st.path.parent.iterdir() if p.name.startswith(".env-")], "no temp files left"


def test_file_store_ignores_foreign_lines(tmp_path):
    p = tmp_path / "env"
    p.write_text("# comment\nOTHER=1\nWINNOW_A=1\nbroken line\n\nWINNOW_B=x=y\n")
    assert userenv.FileEnvStore(p).load() == {"WINNOW_A": "1", "WINNOW_B": "x=y"}


def test_env_file_path_honours_override_and_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("WINNOW_ENV_FILE", str(tmp_path / "x"))
    assert userenv.env_file_path() == tmp_path / "x"
    monkeypatch.delenv("WINNOW_ENV_FILE")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert userenv.env_file_path() == tmp_path / "xdg" / "winnow" / "env"


# --------------------------------------------------------- registry store

class FakeReg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = KEY_SET_VALUE = 0
    REG_SZ = 1

    def __init__(self):
        self.keys = {"Environment": {"Path": "C:\\x", "WINNOW_OLD": "old"}}

    class _H:
        def __init__(self, path): self.path = path

    def CreateKeyEx(self, root, path, _r, _a):
        self.keys.setdefault(path, {})
        return self._H(path)

    def OpenKey(self, root, path, _r, _a):
        if path not in self.keys:
            raise OSError(path)
        return self._H(path)

    def CloseKey(self, h): pass

    def SetValueEx(self, h, name, _r, _kind, value):
        self.keys[h.path][name] = value

    def DeleteValue(self, h, name):
        if name not in self.keys[h.path]:
            raise OSError(name)
        del self.keys[h.path][name]

    def EnumValue(self, h, i):
        items = list(self.keys[h.path].items())
        if i >= len(items):
            raise OSError("no more")
        return items[i][0], items[i][1], self.REG_SZ


def test_registry_store_uses_hkcu_environment_and_broadcasts():
    reg = FakeReg()
    pings = []
    st = userenv.RegistryEnvStore(reg, notify=lambda: pings.append(1))
    assert st.load() == {"WINNOW_OLD": "old"}          # Path is not ours
    st.set("WINNOW_TOKEN", "t")
    assert reg.keys["Environment"]["WINNOW_TOKEN"] == "t"
    assert reg.keys["Environment"]["Path"] == "C:\\x"
    st.delete("WINNOW_OLD")
    st.delete("WINNOW_NEVER")
    assert st.load() == {"WINNOW_TOKEN": "t"}
    assert len(pings) == 3
    assert "Environment" in st.location()


# ------------------------------------------------------------- environ

def test_load_into_environ_lets_the_shell_win(tmp_path, monkeypatch):
    st = userenv.FileEnvStore(tmp_path / "env")
    st.set("WINNOW_FROM_FILE", "file")
    st.set("WINNOW_SHELL_SET", "file")
    monkeypatch.setenv("WINNOW_SHELL_SET", "shell")
    monkeypatch.delenv("WINNOW_FROM_FILE", raising=False)
    assert userenv.load_into_environ(st) == ["WINNOW_FROM_FILE"]
    assert os.environ["WINNOW_FROM_FILE"] == "file"
    assert os.environ["WINNOW_SHELL_SET"] == "shell"


def test_load_into_environ_survives_a_bad_file(tmp_path, capsys):
    class Broken:
        def load(self): raise OSError("nope")
        def location(self): return "here"
    assert userenv.load_into_environ(Broken()) == []
    assert "here" in capsys.readouterr().err


def test_set_get_list_delete_touch_store_and_process(tmp_path, monkeypatch):
    st = userenv.FileEnvStore(tmp_path / "env")
    monkeypatch.setenv("WINNOW_SHELL_ONLY", "s")
    assert userenv.set_var("WINNOW_TOKEN", "abc", st) == "WINNOW_TOKEN"
    assert os.environ["WINNOW_TOKEN"] == "abc" and st.load()["WINNOW_TOKEN"] == "abc"
    assert userenv.get("WINNOW_TOKEN") == "abc"
    assert userenv.get("WINNOW_NOPE", "dflt") == "dflt"
    with pytest.raises(ValueError):
        userenv.get("PATH")
    listed = {v["name"]: v for v in userenv.list_vars(st)}
    assert listed["WINNOW_TOKEN"] == {"name": "WINNOW_TOKEN", "stored": True, "live": True, "reserved": False}
    assert listed["WINNOW_SHELL_ONLY"] == {"name": "WINNOW_SHELL_ONLY", "stored": False, "live": True, "reserved": False}
    assert listed["WINNOW_ENV_FILE"]["reserved"] is True   # the conftest override, live
    userenv.delete_var("WINNOW_TOKEN", st)
    assert "WINNOW_TOKEN" not in os.environ and "WINNOW_TOKEN" not in st.load()
    with pytest.raises(ValueError):
        userenv.set_var("WINNOW_WORKSPACE_DIR", "/elsewhere", st)


def test_default_store_is_the_file_under_the_override(tmp_path):
    st = userenv.store()
    assert isinstance(st, userenv.FileEnvStore)
    assert st.path == userenv.env_file_path()
    assert str(tmp_path) in str(st.path)   # the conftest isolation


# -------------------------------------------------------------- routes

def test_routes_never_return_a_value(client):
    r = client.post("/api/env", json={"name": "WINNOW_TOKEN", "value": "hunter2"})
    assert r.status_code == 200 and r.json() == {"ok": True, "name": "WINNOW_TOKEN"}
    info = client.get("/api/env").json()
    assert "hunter2" not in r.text and "hunter2" not in client.get("/api/env").text
    me = next(v for v in info["vars"] if v["name"] == "WINNOW_TOKEN")
    assert me == {"name": "WINNOW_TOKEN", "stored": True, "live": True, "reserved": False}
    assert info["prefix"] == "WINNOW_" and info["location"]
    assert os.environ["WINNOW_TOKEN"] == "hunter2"
    assert client.delete("/api/env/WINNOW_TOKEN").json() == {"ok": True}
    assert "WINNOW_TOKEN" not in os.environ
    assert all(v["name"] != "WINNOW_TOKEN" for v in client.get("/api/env").json()["vars"])


def test_routes_refuse_bad_names_and_reserved(client):
    assert client.post("/api/env", json={"name": "AWS_SECRET_ACCESS_KEY", "value": "x"}).status_code == 400
    assert client.post("/api/env", json={"name": "WINNOW_WORKSPACE_DIR", "value": "x"}).status_code == 400
    assert client.post("/api/env", json={"name": "WINNOW_X", "value": "a\nb"}).status_code == 400
    assert client.delete("/api/env/PATH").status_code == 400
    assert "PATH" in os.environ


def test_routes_are_loopback_only(client, monkeypatch):
    import server
    monkeypatch.setattr(server, "_is_loopback", lambda request: False)
    assert client.get("/api/env").status_code == 403
    assert client.post("/api/env", json={"name": "WINNOW_T", "value": "x"}).status_code == 403
    assert client.delete("/api/env/WINNOW_T").status_code == 403
    assert "WINNOW_T" not in os.environ


def test_routes_need_the_csrf_header():
    import server
    from fastapi.testclient import TestClient
    bare = TestClient(server.app)
    assert bare.post("/api/env", json={"name": "WINNOW_T", "value": "x"}).status_code == 403
    assert "WINNOW_T" not in os.environ


# --------------------------------------------------------- PluginRequest

def test_plugin_request_env_is_prefix_enforced(monkeypatch):
    monkeypatch.setenv("WINNOW_VT_API_KEY", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "no")
    req = plugin_api.PluginRequest("GET", "x", {}, None, None, {})
    assert req.env("WINNOW_VT_API_KEY") == "k"
    assert req.env("WINNOW_UNSET") is None
    assert req.env("WINNOW_UNSET", "d") == "d"
    with pytest.raises(ValueError):
        req.env("AWS_SECRET_ACCESS_KEY")


def test_a_plugin_route_reads_a_saved_token_but_the_browser_never_sees_it(client, tmp_path, monkeypatch):
    import textwrap
    import server
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "tokdemo.py").write_text(textwrap.dedent("""
        PLUGIN = {"name": "tokdemo", "version": "0.1", "description": "uses a token"}
        def register(api):
            api.register_api("check", lambda req: {"configured": bool(req.env("WINNOW_TOKDEMO_KEY")),
                                                    "len": len(req.env("WINNOW_TOKDEMO_KEY") or "")})
    """))
    reg = plugin_api.PluginRegistry()
    reg.load([pdir])
    monkeypatch.setattr(server, "PLUGINS", reg)
    assert client.get("/api/plugin/tokdemo/check").json() == {"configured": False, "len": 0}
    client.post("/api/env", json={"name": "WINNOW_TOKDEMO_KEY", "value": "abcdef"})
    assert client.get("/api/plugin/tokdemo/check").json() == {"configured": True, "len": 6}
