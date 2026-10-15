"""The browser suite's skip-or-fail switch, and the CI settings that
depend on it.

tests/ui skips when Playwright or Chromium is missing, which is right on an
analyst's machine and wrong in CI: there the required `browser` check
would pass having run nothing. WINNOW_UI_REQUIRE_BROWSER=1, which ci.yml
sets on the browser job, turns that skip into a failure. Both halves run
a real tests/ui test in a subprocess with Playwright pointed at an empty
browser directory, the state a runner is in when its browser install
quietly failed."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TARGET = "tests/ui/test_skin_tokens.py::test_the_picker_offers_more_than_one_style"

pytest.importorskip("playwright.sync_api")


def _run_without_a_browser(tmp_path, require: bool) -> subprocess.CompletedProcess:
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "no-browsers")}
    env.pop("WINNOW_UI_REQUIRE_BROWSER", None)
    if require:
        env["WINNOW_UI_REQUIRE_BROWSER"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", TARGET],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)


def test_without_a_browser_the_ui_suite_skips(tmp_path):
    r = _run_without_a_browser(tmp_path, require=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 skipped" in r.stdout and "chromium unavailable" in r.stdout


def test_ci_turns_a_missing_browser_into_a_failure(tmp_path):
    r = _run_without_a_browser(tmp_path, require=True)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "skipped" not in r.stdout
    assert "chromium unavailable" in r.stdout


def test_ci_requires_the_browser_and_bounds_every_job():
    """A job with no timeout-minutes runs for GitHub's default of six
    hours when a test hangs, and reports nothing until then."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'WINNOW_UI_REQUIRE_BROWSER: "1"' in ci
    assert ci.count("runs-on:") == ci.count("timeout-minutes:")
