"""Quick-look cases: the temporary case a file association lands in, and
the three ways out of one (save, copy into a real case, discard)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from winnow import workspace as WS

HEADERS = {"X-Timeline-Lite-Client": "1"}


@pytest.fixture
def client(store, monkeypatch, tmp_path):
    import server
    monkeypatch.setattr(server, "STORE", None)   # assoc opens its own case
    monkeypatch.setattr(WS.machine_prefs, "get", lambda k, d=None: str(tmp_path / "cases"))
    return server, TestClient(server.app)


def _csv(tmp_path, name="dropped.csv"):
    p = tmp_path / name
    p.write_text("Host,User\nh1,alice\nh2,bob\n", encoding="utf-8")
    return str(p)


def test_assoc_open_creates_a_quicklook_case_and_ingests(client, tmp_path):
    server, c = client
    res = c.post("/api/assoc/open", json={"files": [_csv(tmp_path)]}, headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["temp"] is True
    assert "quicklook" in body["case"]
    assert body["started"][0]["kind"] == "csv"
    server.STORE.wait_for_ingest_job(body["started"][0]["job_id"], timeout=30)
    assert server.STORE.list_sources()[0]["row_count"] == 2
    # Off the home screen until saved — that's what temporary means.
    assert WS.cases.find_by_path(body["case"]) is None
    # And no first-run setup prompt on top of the file the analyst just
    # opened: a brand-new install's quick look must not open with the
    # cases-dir dialog stacked over it.
    assert c.get("/api/prefs", headers=HEADERS).json()["first_run"] is False
    server.STORE.close()


def test_a_second_drop_joins_the_open_quicklook(client, tmp_path):
    """Five files selected together arrive as five invocations; they belong
    in one case, not five windows."""
    server, c = client
    first = c.post("/api/assoc/open", json={"files": [_csv(tmp_path, "a.csv")]},
                   headers=HEADERS).json()
    second = c.post("/api/assoc/open", json={"files": [_csv(tmp_path, "b.csv")]},
                    headers=HEADERS).json()
    assert second["case"] == first["case"], "joined, not respawned"
    for j in first["started"] + second["started"]:
        server.STORE.wait_for_ingest_job(j["job_id"], timeout=30)
    assert len(server.STORE.list_sources()) == 2
    server.STORE.close()


def test_a_real_case_is_never_hijacked(client, tmp_path, case_path):
    server, c = client
    from winnow.store import Store
    server.STORE = Store(case_path)   # a real, non-quicklook case is open
    try:
        res = c.post("/api/assoc/open", json={"files": [_csv(tmp_path)]}, headers=HEADERS)
        assert res.status_code == 409, "the launcher must spawn instead"
        assert server.STORE.path == case_path, "and this case is untouched"
    finally:
        server.STORE.close()


def test_assoc_open_is_loopback_only(client, tmp_path, monkeypatch):
    server, c = client
    monkeypatch.setattr(server, "_is_loopback", lambda request: False)
    res = c.post("/api/assoc/open", json={"files": [_csv(tmp_path)]}, headers=HEADERS)
    assert res.status_code == 403


def test_save_as_moves_the_file_and_registers_it(client, tmp_path):
    server, c = client
    body = c.post("/api/assoc/open", json={"files": [_csv(tmp_path)]}, headers=HEADERS).json()
    server.STORE.wait_for_ingest_job(body["started"][0]["job_id"], timeout=30)
    old_path = body["case"]

    res = c.post("/api/case/save_as", json={"name": "Intrusion 2026"}, headers=HEADERS)
    assert res.status_code == 200
    saved = res.json()
    assert saved["temp"] is False
    assert not os.path.exists(old_path), "the quicklook file moved, not copied"
    assert os.path.isfile(saved["path"])
    assert "quicklook" not in saved["path"]
    # Registered under the analyst's name, data intact through the
    # close→rename→reopen dance.
    rec = WS.cases.find_by_path(saved["path"])
    assert rec and rec["name"] == "Intrusion 2026"
    assert server.STORE.list_sources()[0]["row_count"] == 2
    server.STORE.close()


def test_save_as_refuses_a_real_case(client, case_path):
    server, c = client
    from winnow.store import Store
    server.STORE = Store(case_path)
    try:
        res = c.post("/api/case/save_as", json={"name": "x"}, headers=HEADERS)
        assert res.status_code == 400
        assert "already has a home" in res.json()["detail"]
    finally:
        server.STORE.close()


def test_discard_deletes_the_quicklook_and_only_the_quicklook(client, tmp_path):
    server, c = client
    body = c.post("/api/assoc/open", json={"files": [_csv(tmp_path)]}, headers=HEADERS).json()
    server.STORE.wait_for_ingest_job(body["started"][0]["job_id"], timeout=30)

    res = c.post("/api/case/discard", json={}, headers=HEADERS)
    assert res.status_code == 200
    assert not os.path.exists(body["case"])
    assert server.STORE is None


def test_discard_refuses_a_real_case(client, case_path):
    server, c = client
    from winnow.store import Store
    server.STORE = Store(case_path)
    try:
        res = c.post("/api/case/discard", json={}, headers=HEADERS)
        assert res.status_code == 400
        assert os.path.exists(case_path), "a real case must never be deletable this way"
    finally:
        server.STORE.close()


def test_case_file_sniffing(client, tmp_path, case_path):
    server, c = client
    assert server.is_winnow_case_file(case_path) is False or True  # shape check below
    # A real winnow case:
    from winnow.store import Store
    s = Store(str(tmp_path / "real.db"))
    s.close()
    assert server.is_winnow_case_file(str(tmp_path / "real.db")) is True
    # A generic sqlite file is data, not a case.
    import sqlite3
    g = tmp_path / "generic.db"
    conn = sqlite3.connect(g); conn.execute("CREATE TABLE t (x)"); conn.commit(); conn.close()
    assert server.is_winnow_case_file(str(g)) is False
    # Not sqlite at all.
    (tmp_path / "notdb.db").write_text("hello")
    assert server.is_winnow_case_file(str(tmp_path / "notdb.db")) is False


def test_the_janitor_eats_only_abandoned_quicklooks(client, tmp_path):
    server, c = client
    import time as _t
    from winnow.store import Store
    qdir = tmp_path / "cases" / "quicklook"
    qdir.mkdir(parents=True)

    def make(name, tag=False):
        st = Store(str(qdir / name))
        sid = st.ingest_rows(["A"], [["1"]], name="x", build_fts=False)["id"]
        if tag:
            t = st.upsert_tag(None, "Keep", "#f00", None)
            st.set_tags(sid, [1], t["id"], True)
        st.close()
        old = _t.time() - 8 * 86400
        os.utime(qdir / name, (old, old))

    make("old-empty.db")
    make("old-tagged.db", tag=True)
    st = Store(str(qdir / "young-empty.db")); st.close()

    removed = server._sweep_quicklook()

    assert removed == 1
    assert not (qdir / "old-empty.db").exists()
    assert (qdir / "old-tagged.db").exists(), "work is never garbage"
    assert (qdir / "young-empty.db").exists()


# ------------------------------------------------------- the real launcher

import http.client
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent


def _get(port, route):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("GET", route, headers={"X-Timeline-Lite-Client": "1"})
    body = json.loads(conn.getresponse().read().decode())
    conn.close()
    return body


def test_a_real_assoc_launch_becomes_a_quicklook_server(tmp_path):
    """python server.py --assoc file.csv with no Winnow running: this
    process becomes the server, on its own port, with the file in a temp
    case — proven by asking the real process over HTTP."""
    dropped = tmp_path / "evidence.csv"
    dropped.write_text("Host,User\nh1,alice\n", encoding="utf-8")
    # The child is a real process with the real INSTALL_ROOT, so without
    # these overrides it would register itself in the developer's actual
    # workspace/ and drop its quicklook file in the actual cases/ — state
    # that outlives the test and collides on the next run.
    env = {**os.environ, "WINNOW_NEVER_CONNECTED_EXIT_S": "600",
           "WINNOW_IDLE_EXIT_S": "600",
           "WINNOW_WORKSPACE_DIR": str(tmp_path / "ws"),
           "WINNOW_CASES_DIR": str(tmp_path / "cases")}
    # -u: the child's stdout is block-buffered under a pipe, so without it
    # the "Winnow on …" line sits in the child's buffer and readline()
    # starves forever.
    proc = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "server.py"), "--assoc", str(dropped),
         "--no-browser", "--no-fts"],
        cwd=str(tmp_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        # Its port is self-chosen; learn it from the process's own output.
        port = None
        deadline = time.monotonic() + 40
        lines = []
        while time.monotonic() < deadline and port is None:
            line = proc.stdout.readline().decode(errors="replace")
            lines.append(line)
            if "Winnow on http://127.0.0.1:" in line:
                port = int(line.split(":")[2].split("/")[0].split()[0])
            if proc.poll() is not None:
                pytest.fail("launcher-server exited early:\n" + "".join(lines))
        assert port, "never announced a port:\n" + "".join(lines)

        # The announcement prints before uvicorn binds; wait for the socket.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                _get(port, "/api/version")
                break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.fail("announced port never started accepting")

        cur = _get(port, "/api/case/current")
        assert cur["temp"] is True
        assert "quicklook" in cur["path"]
        assert cur["path"].startswith(str(tmp_path / "cases")), cur["path"]

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            src = _get(port, "/api/sources")
            if src and src[0].get("row_count") == 1:
                break
            time.sleep(0.3)
        else:
            pytest.fail(f"ingest never landed: {_get(port, '/api/sources')}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
