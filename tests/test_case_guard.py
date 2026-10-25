"""A window only ever reads and writes the case it is showing.

One server has one case open, and any number of windows can point at it.
When one of them opened another case, the rest went on showing the old one
and sending requests that named rows by numbers every case reuses — source
1, rid 37, tag 2 — so a window left on case A, after another window opened
case B on the same server, painted B's rows under A's tabs and tagged B's
rows on whichever line had the same rid. Reported as tags "from one case"
showing up in the other, on the wrong lines.

Every page request now carries the case its state belongs to (the
X-Winnow-Case header; `case_token` in the query for a download), and the
server refuses one naming any other case before it touches anything.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time

import pytest

import server
from spawn import running_server
from winnow import workspace as WS
from winnow.store import DEFAULT_TAGS, Store

ROWS = [["ts", "msg"], *[[f"2026-01-01T00:00:{i:02d}", f"row {i}"] for i in range(20)]]


def _case(tmp_path, write_csv, name):
    """A closed case with one imported table, registered on the home screen."""
    path = str(tmp_path / f"{name}.db-winnow")
    st = Store(path, default_tags=DEFAULT_TAGS)
    st.ingest_csv(write_csv(ROWS, f"{name}.csv"), name=f"{name}.csv", build_fts=False)
    st.close()
    WS.cases.create(path, name=name)
    return path


def _tagged(path) -> set[tuple[int, int, int]]:
    st = Store(path, default_tags=DEFAULT_TAGS)
    try:
        return {tuple(r) for r in st.db.execute("SELECT source_id, rid, tag_id FROM row_tags")}
    finally:
        st.close()


@pytest.fixture
def two_cases(client, tmp_path, write_csv):
    """Case A open in this "window" (its token), then case B opened on the
    same server — by another window."""
    a, b = _case(tmp_path, write_csv, "alpha"), _case(tmp_path, write_csv, "bravo")
    opened = client.post("/api/case/open", json={"path": a})
    assert opened.status_code == 200, opened.text
    token_a = opened.json()["case_token"]
    assert client.get("/api/case/current").json()["case_token"] == token_a
    other = client.post("/api/case/open", json={"path": b})
    assert other.status_code == 200, other.text
    token_b = other.json()["case_token"]
    assert token_a and token_b and token_a != token_b
    return a, b, token_a, token_b


def test_a_tag_sent_for_the_case_another_window_left_is_refused(client, two_cases):
    a, b, token_a, token_b = two_cases
    tag = client.get("/api/tags").json()["tags"][0]["id"]
    r = client.post("/api/row_tags", json={"source_id": 1, "rids": [3, 4], "tag_id": tag, "on": True},
                    headers={"X-Winnow-Case": token_a})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "case_changed"
    assert detail["open"] == {"token": token_b, "name": "bravo"}
    assert "bravo" in detail["message"]
    # Refused before the route ran: neither case has the tag.
    server.STORE.close()
    assert _tagged(b) == set()
    assert _tagged(a) == set()


def test_reads_for_the_old_case_are_refused_too(client, two_cases):
    """Painting B's rows under A's tabs is how the wrong-line tags started."""
    _, _, token_a, token_b = two_cases
    assert client.get("/api/sources", headers={"X-Winnow-Case": token_a}).status_code == 409
    mine = client.get("/api/sources", headers={"X-Winnow-Case": token_b})
    assert mine.status_code == 200
    assert [s["name"] for s in mine.json()] == ["bravo.csv"]


def test_an_import_for_the_old_case_is_refused(client, two_cases, write_csv):
    _, b, token_a, _ = two_cases
    path = write_csv(ROWS, "late.csv")
    with open(path, "rb") as f:
        r = client.post("/api/ingest/upload", files={"file": ("late.csv", f, "text/csv")},
                        headers={"X-Winnow-Case": token_a})
    assert r.status_code == 409, r.text
    assert [s["name"] for s in client.get("/api/sources").json()] == ["bravo.csv"]


def test_a_download_for_the_old_case_is_a_page_saying_so(client, two_cases):
    """Downloads are plain navigations: the token rides in the query, and
    the refusal replaces the app, so it is a page with a way back."""
    _, _, token_a, token_b = two_cases
    r = client.get(f"/api/export/tagged_xlsx?case_token={token_a}")
    assert r.status_code == 409
    assert r.headers["content-type"].startswith("text/html")
    assert "out of date" in r.text and "href='/'" in r.text
    assert client.get(f"/api/export/tagged_xlsx?case_token={token_b}").status_code == 200


def test_the_switch_and_the_home_screen_still_answer_a_stale_window(client, two_cases):
    """What a window may still do while its case isn't the one open: go
    home, list cases, open one. Otherwise it could never get back."""
    a, _, token_a, _ = two_cases
    stale = {"X-Winnow-Case": token_a}
    assert client.get("/api/cases", headers=stale).status_code == 200
    assert client.get("/api/case/current", headers=stale).status_code == 200
    assert client.get("/api/version", headers=stale).status_code == 200
    back = client.post("/api/case/open", json={"path": a}, headers=stale)
    assert back.status_code == 200
    assert back.json()["case_token"] == token_a
    assert client.get("/api/sources", headers=stale).status_code == 200


def test_a_request_that_names_no_case_is_let_through(client, two_cases):
    """Scripts, the launcher, and pages from before the token existed."""
    assert client.get("/api/sources").status_code == 200


def test_the_token_names_the_file_not_the_open(client, tmp_path, write_csv):
    """A window that outlives a restart of its server on the same case goes
    on working, as it always has: the token is the case file's, however
    its path is spelled."""
    a = _case(tmp_path, write_csv, "alpha")
    first = client.post("/api/case/open", json={"path": a}).json()["case_token"]
    server.STORE.close()                    # the server went away...
    again = client.post("/api/case/open", json={"path": a}).json()["case_token"]
    assert again == first                   # ...and came back on the same case
    if hasattr(os, "symlink"):
        link = tmp_path / "link.db-winnow"
        os.symlink(a, link)
        assert server._case_token_for(str(link)) == first


def test_no_case_open_refuses_a_window_that_had_one(client, tmp_path, write_csv):
    a = _case(tmp_path, write_csv, "alpha")
    token = client.post("/api/case/open", json={"path": a}).json()["case_token"]
    server.STORE.close()
    r = client.get("/api/sources", headers={"X-Winnow-Case": token})
    assert r.status_code == 409
    assert r.json()["detail"]["open"] is None


def test_saving_a_quick_look_hands_its_window_the_new_token(client, tmp_path, monkeypatch):
    """Save-as moves the file, so the case's token moves with it; the page
    that saved adopts the one the answer names."""
    monkeypatch.setenv("WINNOW_CASES_DIR", str(tmp_path / "cases"))
    path = client.post("/api/case/quicklook/new", json={}).json()["path"]
    before = client.post("/api/case/open", json={"path": path}).json()["case_token"]
    saved = client.post("/api/case/save_as", json={"name": "kept", "path": str(tmp_path / "kept.db-winnow")},
                        headers={"X-Winnow-Case": before})
    assert saved.status_code == 200, saved.text
    after = saved.json()["case_token"]
    assert after and after != before
    assert client.get("/api/sources", headers={"X-Winnow-Case": after}).status_code == 200
    gone = client.post("/api/case/discard", json={}, headers={"X-Winnow-Case": after})
    assert gone.status_code == 400   # a saved case isn't discarded; the point is it wasn't refused


def test_two_quick_looks_started_in_the_same_second_get_their_own_files(tmp_path, monkeypatch):
    """The name was the time to the second, checked before the file
    existed, so two Winnows started by double-clicks in one second shared
    a quick look."""
    monkeypatch.setenv("WINNOW_CASES_DIR", str(tmp_path / "cases"))
    mine = server._new_temp_case_path()
    other = os.getpid() + 1
    monkeypatch.setattr(server.os, "getpid", lambda: other)   # a second Winnow, same second
    theirs = server._new_temp_case_path()
    assert mine != theirs


def test_named_session_files_are_kept_per_case(tmp_path, write_csv):
    """sessions/ beside the case was shared by every case in the folder:
    one case listed, and could load, another's sessions."""
    a = Store(str(tmp_path / "alpha.db-winnow"), default_tags=DEFAULT_TAGS)
    b = Store(str(tmp_path / "bravo.db-winnow"), default_tags=DEFAULT_TAGS)
    try:
        a.ingest_csv(write_csv(ROWS, "a.csv"), build_fts=False)
        a.save_named_session("alpha-review")
        assert [s["name"] for s in a.list_named_sessions()] == ["alpha-review"]
        assert b.list_named_sessions() == []
        with pytest.raises(KeyError):
            b.load_named_session("alpha-review")
    finally:
        a.close()
        b.close()


# ------------------------------------------------- told over the presence stream

def _events(port, stop, out):
    """Collect `case` events from a presence stream until `stop` is set."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", "/api/presence?page=guard-test")
    resp = conn.getresponse()
    event = None
    while not stop.is_set():
        line = resp.fp.readline().decode()
        if not line:
            break
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: ") and event == "case":
            out.append(json.loads(line[6:]))
            event = None
    conn.close()


def _wait(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_every_window_hears_at_once_that_another_switched_the_case(tmp_path, write_csv):
    """Before it asks for anything: the stream says which case is open when
    it opens, and again the moment a switch lands — not at the next 15s
    ping."""
    a, b = str(tmp_path / "alpha.db-winnow"), str(tmp_path / "bravo.db-winnow")
    for p in (a, b):
        Store(p, default_tags=DEFAULT_TAGS).close()
    with running_server(tmp_path / "srv", "--case", a) as srv:
        seen, stop = [], threading.Event()
        t = threading.Thread(target=_events, args=(srv.port, stop, seen), daemon=True)
        t.start()
        try:
            _wait(lambda: len(seen) == 1)
            assert seen[0]["name"] == "alpha"
            assert seen[0]["token"] == srv.get("/api/case/current")["case_token"]
            switched = time.monotonic()
            srv.post("/api/case/open", {"path": b})
            _wait(lambda: len(seen) == 2, timeout=5)
            assert time.monotonic() - switched < 5
            assert seen[1]["name"] == "bravo"
            assert seen[1]["token"] != seen[0]["token"]
        finally:
            stop.set()
