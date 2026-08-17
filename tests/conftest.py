"""Shared fixtures for the backend test suite.

Every test gets its own tmp_path-backed case file and workspace/ directory —
nothing here ever touches the repo's real case.db, sample.csv or workspace/.
"""

from __future__ import annotations

import csv as csv_module
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import workspace as WS  # noqa: E402
from store import DEFAULT_TAGS, Store  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_workspace(tmp_path, monkeypatch):
    """Every workspace/*.json store (cases, filters, tags, column layouts)
    is redirected to a per-test tmp dir — so tests that go through server.py
    routes (which read/write WS.* directly) can't leak into or read from the
    developer's real workspace/ directory."""
    monkeypatch.setattr(WS, "WORKSPACE_DIR", tmp_path / "workspace")


@pytest.fixture
def case_path(tmp_path) -> str:
    return str(tmp_path / "case.db")


@pytest.fixture
def write_csv(tmp_path):
    """Factory fixture: write_csv(rows, name="f.csv") -> path. `rows`
    includes the header row (or not, for has_header=False tests)."""

    def _write(rows: list[list[str]], name: str = "fixture.csv") -> str:
        path = tmp_path / name
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv_module.writer(f)
            w.writerows(rows)
        return str(path)

    return _write


@pytest.fixture
def store(case_path):
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    yield s
    s.close()


STANDARD_ROWS = [
    ["Timestamp", "EventId", "User", "Process", "CommandLine"],
    ["2024-01-05 13:22:01", "4624", "ACME\\jacson", "svchost.exe", "C:\\Windows\\System32\\svchost.exe"],
    ["2024-01-05 13:23:11", "4688", "ACME\\admin", "cmd.exe", "C:\\Windows\\System32\\cmd.exe"],
    ["2024-01-06 09:15:00", "4625", "ACME\\bob", "powershell.exe", "C:\\users\\jacso\\desktop\\file.txt"],
    ["2024-01-07 22:01:59", "1", "NT AUTHORITY\\SYSTEM", "lsass.exe", "C:\\Windows\\System32\\lsass.exe"],
]


@pytest.fixture
def ingested(store, write_csv):
    """Ingests the standard small fixture (same shape used for manual
    verification during development) and returns (store, source_id)."""
    path = write_csv(STANDARD_ROWS)
    rec = store.ingest_csv(path, name="standard.csv")
    return store, rec["id"]


@pytest.fixture
def client(store, monkeypatch):
    import server

    monkeypatch.setattr(server, "STORE", store)
    from fastapi.testclient import TestClient

    yield TestClient(server.app, headers={"X-Timeline-Lite-Client": "1"})
    # A test that goes through /api/case/open leaves server.STORE pointing at
    # a Store that route created. monkeypatch puts the attribute back but
    # can't close that object, and an unclosed Store keeps its views database
    # in /dev/shm (or the platform tempdir) forever — the suite was leaking
    # one file per run, which is the exact shape of the leak the sweep in
    # test_maintenance.py exists to clean up.
    opened = server.STORE
    if opened is not None and opened is not store:
        opened.close()
