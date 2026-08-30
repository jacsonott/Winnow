"""The running-instances registry, and the cross-process workspace lock
underneath it.

Both exist for the same reason: file associations make a second server a
supported flow, and neither "which Winnow is running where" nor "two
processes writing cases.json" had an answer before. The lock test is the
load-bearing one — it proves lost-update corruption is actually prevented,
not just discouraged."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from winnow import instances, workspace as WS


def test_register_set_case_unregister_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(WS, "WORKSPACE_DIR", tmp_path / "ws")

    instances.register(8777, None)
    always = lambda p: True
    assert [i["port"] for i in instances.running(_probe=always)] == [8777]
    assert instances.find_idle(_probe=always)["port"] == 8777

    instances.set_case("/evidence/case.db")
    assert instances.find_idle(_probe=always) is None, "a busy server is not reusable"
    assert instances.running(_probe=always)[0]["case_path"].endswith("case.db")

    instances.set_case(None)
    assert instances.find_idle(_probe=always)["port"] == 8777

    instances.unregister()
    assert instances.running(_probe=always) == []


def test_dead_entries_are_pruned_by_the_probe_not_trusted(tmp_path, monkeypatch):
    """A crashed server leaves its entry behind, and PIDs get reused — the
    file is a hint, the port is the truth."""
    monkeypatch.setattr(WS, "WORKSPACE_DIR", tmp_path / "ws")
    WS._write(instances.FILE, {"instances": [
        {"pid": 1, "port": 1111, "case_path": None, "started_at": "x"},
        {"pid": 2, "port": 2222, "case_path": None, "started_at": "x"},
        {"pid": 3, "port": "not-a-port", "case_path": None, "started_at": "x"},
    ]})

    alive = instances.running(_probe=lambda p: p == 2222)

    assert [i["port"] for i in alive] == [2222]
    # And the pruning was written back, so ghosts don't accumulate.
    on_disk = json.loads((tmp_path / "ws" / instances.FILE).read_text())
    assert [i["port"] for i in on_disk["instances"]] == [2222]


def test_a_corrupt_registry_reads_as_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(WS, "WORKSPACE_DIR", tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / instances.FILE).write_text("{not json")
    assert instances.running(_probe=lambda p: True) == []


WORKER = '''
import json, os, sys
sys.path.insert(0, {root!r})
from pathlib import Path
from winnow import workspace as WS
WS.WORKSPACE_DIR = Path({ws!r})

# Read-modify-write, the pattern every workspace store uses. Without the
# cross-process lock, concurrent workers interleave and drop each other's
# appends.
for n in range(25):
    with WS._LOCK:
        try:
            with open(WS.WORKSPACE_DIR / "contended.json", encoding="utf-8") as f:
                items = json.load(f)["items"]
        except (OSError, ValueError):
            items = []
        items.append(f"{{os.getpid()}}-{{n}}")
        WS._write("contended.json", {{"items": items}})
'''


def test_concurrent_processes_do_not_lose_writes(tmp_path):
    """Four processes, 25 appends each: exactly 100 entries must survive.
    Against the old in-process-only RLock this loses a large fraction of
    them — both processes read the same list and the last writer wins."""
    root = str(Path(__file__).resolve().parent.parent)
    ws = str(tmp_path / "ws")
    script = WORKER.format(root=root, ws=ws)
    procs = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(4)]
    for p in procs:
        assert p.wait(timeout=60) == 0

    items = json.loads((tmp_path / "ws" / "contended.json").read_text())["items"]
    assert len(items) == 100, f"lost {100 - len(items)} of 100 writes"
    assert len(set(items)) == 100
