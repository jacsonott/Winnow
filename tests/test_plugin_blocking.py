"""A slow plugin route must not stop the rest of Winnow.

Reported by a plugin author whose LLM calls take seconds: while one was in
flight the whole app was unresponsive. The dispatcher was `async def` and
called the handler directly, so a blocking handler owned the event loop
and every other request — the grid, the presence stream — waited behind it.
Handlers now run in a worker thread.

Driven against a real uvicorn, because that is the thing being tested: an
in-process TestClient does not have the event loop this used to block.
"""

from __future__ import annotations

import http.client
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

SLOW_PLUGIN = '''
    import time

    PLUGIN = {"name": "slowpoke", "version": "0.1", "description": "blocks like a network call"}

    def _slow(req):
        time.sleep(float(req.query.get("s", "3")))   # a remote service, from Winnow's side
        return {"ok": True}

    def register(api):
        api.register_api("wait", _slow, methods=("GET",))
'''


@pytest.fixture
def server_with_slow_plugin(tmp_path):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "slowpoke.py").write_text(textwrap.dedent(SLOW_PLUGIN))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ,
           "WINNOW_WORKSPACE_DIR": str(tmp_path / "ws"),
           "WINNOW_ENV_FILE": str(tmp_path / "userenv"),
           "WINNOW_CASES_DIR": str(tmp_path / "cases"),
           "WINNOW_PLUGINS_DIR": str(pdir),
           "WINNOW_IDLE_EXIT_S": "300", "WINNOW_NEVER_CONNECTED_EXIT_S": "300"}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--case", str(tmp_path / "c.db"),
         "--port", str(port), "--host", "127.0.0.1", "--no-browser", "--no-fts"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail("server exited during startup:\n"
                        + proc.stdout.read().decode(errors="replace"))
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/api/version")
            c.getresponse().read()
            c.close()
            break
        except OSError:
            time.sleep(0.15)
    else:
        proc.kill()
        pytest.fail("server did not come up")
    yield port
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _get(port, path, timeout=30):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    c.request("GET", path)
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def test_the_app_answers_while_a_plugin_waits_on_a_slow_service(server_with_slow_plugin):
    port = server_with_slow_plugin
    started = threading.Event()

    slow_status = []

    def slow():
        started.set()
        slow_status.append(_get(port, "/api/plugin/slowpoke/wait?s=3"))

    t = threading.Thread(target=slow, daemon=True)
    t.start()
    started.wait(5)
    time.sleep(0.4)                       # the handler is now inside its sleep

    # Ordinary requests must answer immediately, not after the plugin does.
    t0 = time.monotonic()
    assert _get(port, "/api/version", timeout=10) == 200
    assert _get(port, "/api/sources", timeout=10) == 200
    took = time.monotonic() - t0
    assert took < 1.5, f"the app was blocked by the plugin: {took:.1f}s for two requests"
    t.join(timeout=20)
    # Without this the test passes vacuously if the plugin stops loading:
    # a 404 answers in ~1ms and satisfies every timing bound above.
    assert slow_status == [200], f"the slow plugin route did not run: {slow_status}"


def test_two_slow_plugin_calls_overlap_rather_than_queue(server_with_slow_plugin):
    """Threadpool, not a single worker: a second analyst action while one
    call is in flight should not wait for the first."""
    port = server_with_slow_plugin
    done = []

    def call():
        status = _get(port, "/api/plugin/slowpoke/wait?s=2")
        done.append((time.monotonic(), status))

    t0 = time.monotonic()
    threads = [threading.Thread(target=call, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=25)
    assert len(done) == 2, "a slow plugin call did not finish"
    assert [st for _, st in done] == [200, 200], f"the plugin route did not run: {done}"
    assert max(t for t, _ in done) - t0 < 3.5, "the two calls were serialised"
    assert max(t for t, _ in done) - t0 >= 2, "the plugin did not actually block for its 2s"


def test_slow_plugins_cannot_starve_the_rest_of_the_app(server_with_slow_plugin):
    """Moving handlers off the loop is not enough on its own: run_in_threadpool
    uses anyio's DEFAULT limiter, shared with every sync route, so enough
    simultaneous slow plugin calls would fill it and stall the app anyway.
    Plugin handlers have a limiter of their own (server.PLUGIN_THREADS), so
    core routes keep answering however many are queued."""
    port = server_with_slow_plugin
    threads = [threading.Thread(target=lambda: _get(port, "/api/plugin/slowpoke/wait?s=4"),
                                daemon=True) for _ in range(20)]
    for t in threads:
        t.start()
    time.sleep(1.0)                      # all of them are in the handler now

    t0 = time.monotonic()
    assert _get(port, "/api/version", timeout=10) == 200
    assert _get(port, "/api/sources", timeout=10) == 200
    took = time.monotonic() - t0
    assert took < 2.0, f"20 slow plugin calls starved the app: {took:.1f}s for two requests"
    for t in threads:
        t.join(timeout=30)


def test_the_plugin_limiter_is_smaller_than_the_shared_pool():
    """The number itself: bigger than the default (40) would defeat it."""
    import asyncio

    import anyio

    import server

    async def default_tokens():
        return anyio.to_thread.current_default_thread_limiter().total_tokens

    shared = asyncio.run(default_tokens())
    assert server.PLUGIN_THREADS.total_tokens < shared, (
        f"the plugin limiter ({server.PLUGIN_THREADS.total_tokens}) has to leave room in the "
        f"shared pool ({shared}) for everything else")
