"""Which Winnow servers are running on this machine, and where.

File associations need to answer two questions before opening anything:
is a Winnow already running, and is it busy? Neither was answerable —
the case lock records user/host/pid but not the port, and nothing else
records that a server exists at all. This registry does: each server
writes itself into workspace/instances.json on startup (port, pid, which
case it has open), keeps the case field current, and removes itself on
shutdown.

**The file is a hint; the port is the truth.** A crashed server leaves
its entry behind, and PIDs get reused, so entries are verified by asking
the port for /api/version and checking that something Winnow-shaped
answers. running() prunes what doesn't — which is also what keeps the
file from accumulating ghosts, since every launcher consults it through
running().

An "idle" instance is one with no case open. That is the only kind an
association-launch may reuse: a server has exactly one open case
(server.py's STORE is a process global), so handing a busy server a new
case would switch every window that analyst has open, mid-triage.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from . import workspace as WS

FILE = "instances.json"


def _read() -> list[dict]:
    try:
        with open(WS.WORKSPACE_DIR / FILE, encoding="utf-8") as f:
            data = json.load(f)
        return list(data.get("instances") or [])
    except (OSError, ValueError):
        return []


def _write(items: list[dict]) -> None:
    WS._write(FILE, {"instances": items})


def probe(port: int, timeout: float = 0.6) -> bool:
    """Is the thing on this port a live Winnow? /api/version is cheap,
    unauthenticated, and returns a shape nothing else on a loopback port
    is likely to."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=timeout) as r:
            return "version" in json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — any failure means "not a live Winnow"
        return False


def register(port: int, case_path: str | None) -> None:
    with WS._LOCK:
        items = [i for i in _read() if i.get("pid") != os.getpid()]
        items.append({
            "pid": os.getpid(),
            "port": port,
            "case_path": os.path.abspath(case_path) if case_path else None,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        _write(items)


def set_case(case_path: str | None) -> None:
    """Keep this process's entry honest as cases open and switch."""
    with WS._LOCK:
        items = _read()
        for i in items:
            if i.get("pid") == os.getpid():
                i["case_path"] = os.path.abspath(case_path) if case_path else None
        _write(items)


def unregister() -> None:
    with WS._LOCK:
        _write([i for i in _read() if i.get("pid") != os.getpid()])


def running(_probe=None) -> list[dict]:
    """Every instance that actually answers, ghosts pruned. `_probe` is the
    test seam — the suite must not depend on real sockets timing out."""
    check = _probe or probe
    with WS._LOCK:
        items = _read()
        alive = [i for i in items if isinstance(i.get("port"), int) and check(i["port"])]
        if len(alive) != len(items):
            _write(alive)
    return alive


def find_idle(_probe=None) -> dict | None:
    """The reusable instance, if any: running, and with no case open."""
    for i in running(_probe=_probe):
        if not i.get("case_path"):
            return i
    return None
