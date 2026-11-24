"""Traps that make a UI test pass without testing anything.

A UI test that silently does nothing is worse than no test: it reports
green, and the thing it was written to catch keeps happening. These are
checked statically because both traps look completely reasonable on the
page — they read as careful waiting.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent / "ui"
FILES = sorted(UI.glob("test_*.py"))


def test_there_are_ui_tests_to_check():
    assert len(FILES) > 20, "the scan found almost no UI tests — has the layout changed?"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_wait_on_a_promise_predicate(path):
    """`page.wait_for_function` does NOT await its predicate. Hand it
    `() => fetch(...).then(...)` and it receives a Promise, which is
    truthy, so the wait returns immediately and the test races whatever it
    meant to wait for.

    This has bitten four times, most recently as a merge-builder test that
    failed only on loaded CI runners — where the import it wasn't waiting
    for was slow enough to lose the race. Poll from Python with
    `page.evaluate`, which does await.
    """
    src = path.read_text(encoding="utf-8")
    hits = []
    # [rRfF]*: an f-string predicate is one too. This scan skipped them,
    # and the one fetch-in-a-wait it missed came to light only when ruff
    # took the `f` off a placeholder-free f-string.
    for m in re.finditer(r"wait_for_function\(\s*[rRfF]*(\"\"\"|['\"])(.*?)\1", src, re.S):
        pred = m.group(2)
        if ".then(" in pred or re.search(r"\b(async|await)\b", pred):
            hits.append(src[:m.start()].count("\n") + 1)
    assert not hits, (
        f"{path.name} waits on a promise-returning predicate at line(s) {hits} — "
        "that wait passes instantly. Poll with page.evaluate instead."
    )


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_bare_expect_without_assertion(path):
    """`page.locator(...)` on its own line does nothing — no wait, no
    check. It reads like an assertion and is one only in Playwright's
    `expect()` form, which this suite does not use."""
    bad = [i + 1 for i, line in enumerate(path.read_text(encoding="utf-8").splitlines())
           if re.match(r"^\s*page\.locator\([^)]*\)\s*$", line)]
    assert not bad, f"{path.name}: bare locator with no assertion at line(s) {bad}"


def _fixed_waits_then_asserts(src: str) -> list[int]:
    lines = src.splitlines()
    hits = []
    for node in ast.walk(ast.parse(src)):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(node, field, None)
            if not isinstance(body, list):
                continue
            for wait, nxt in zip(body, body[1:]):
                call = wait.value if isinstance(wait, ast.Expr) else None
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "wait_for_timeout"):
                    continue
                if not isinstance(nxt, ast.Assert):
                    continue
                said = "#" in lines[wait.lineno - 1] or lines[wait.lineno - 2].strip().startswith("#")
                if not said:
                    hits.append(wait.lineno)
    return hits


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_unexplained_fixed_wait_before_an_assert(path):
    """`page.wait_for_timeout(n)` then `assert` is a guess at how long the
    app takes. Too short and the test fails on a loaded CI runner; long
    enough and it is slow everywhere else. Either wait for the thing being
    asserted (`until`, `painted`, `view_rebuilt`, `copied` and the rest in
    tests/ui/conftest.py), or, when the wait is the point (a check that
    something did NOT happen has nothing to wait for), say so in a comment
    on the wait or the line above it."""
    hits = _fixed_waits_then_asserts(path.read_text(encoding="utf-8"))
    assert not hits, (
        f"{path.name}: a fixed wait goes straight into an assert at line(s) {hits}. "
        "Wait for what the assert checks, or comment why a fixed wait is right."
    )


def test_the_fixed_wait_check_sees_what_it_is_for():
    flagged = _fixed_waits_then_asserts(
        "def t(page):\n"
        "    page.wait_for_timeout(300)\n"
        "    assert page.evaluate('1')\n"
    )
    assert flagged == [2]
    explained = _fixed_waits_then_asserts(
        "def t(page):\n"
        "    # a second click would have opened it by now\n"
        "    page.wait_for_timeout(300)\n"
        "    assert page.evaluate('1')\n"
        "    page.wait_for_timeout(300)   # nothing to wait for\n"
        "    assert page.evaluate('1')\n"
    )
    assert explained == []


def _waits_on_faked_requests(src: str) -> list[int]:
    """Lines that `expect_request` a URL the same module answers with a
    fake (a module that fulfills, routing that URL)."""
    if ".fulfill(" not in src:
        return []   # its routes hold or pass requests on: nothing is faked
    tree = ast.parse(src)
    lines = src.splitlines()
    comments = {tok.start[0]: tok.string for tok in tokenize.generate_tokens(io.StringIO(src).readline)
                if tok.type == tokenize.COMMENT}
    named = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            named[node.name] = ast.get_source_segment(src, node)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            named[node.targets[0].id] = ast.get_source_segment(src, node.value)

    def says(arg):
        """What a URL argument amounts to: a string, or the source of the
        name or the helper call that stands for one."""
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.Name):
            return named.get(arg.id, "")
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
            return named.get(arg.func.id, "")
        return ast.get_source_segment(src, arg) or ""

    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    faked = set()
    for call in calls:
        if isinstance(call.func, ast.Attribute) and call.func.attr == "route" and call.args:
            url = call.args[0]
        elif isinstance(call.func, ast.Name) and call.func.id == "fake_route" and len(call.args) > 1:
            url = call.args[1]
        else:
            continue
        if isinstance(url, ast.Name) and url.id in named:
            url = ast.parse(named[url.id], mode="eval").body
        if isinstance(url, ast.Constant) and isinstance(url.value, str) and url.value.strip("*"):
            faked.add(url.value.strip("*"))
    hits = []
    for call in calls:
        if not (isinstance(call.func, ast.Attribute) and call.func.attr == "expect_request" and call.args):
            continue
        if not any(path in says(call.args[0]) for path in faked):
            continue
        # Excused by a comment that names expect_request, on the line or
        # in the comment block straight above it: a comment that is only
        # nearby says nothing about the wait.
        above = call.lineno - 1
        while above > 1 and lines[above - 2].strip().startswith("#"):
            above -= 1
        if not any("expect_request" in comments.get(n, "") for n in range(above, call.lineno + 1)):
            hits.append(call.lineno)
    return sorted(set(hits))


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_wait_on_a_faked_request_rather_than_its_answer(path):
    """`page.expect_request` returns as the request leaves, before the
    route that fakes its answer has run. A test that goes on from there
    and removes the route (a fixture's teardown) hands the request to the
    real server: test_custom_delimiter_ui.py's faked import ran for real
    that way and left its table for every later module in the nightly's
    flake hunt, and a loop doing the same leaked 148 times in 150. Wait
    with `expect_response` and read `.request` off it; a fixture that
    fakes a route uses `fake_route`, whose teardown waits for its
    answers. A request a route holds, to answer by hand later, has no
    response to wait for: say so in a comment that starts
    `# expect_request:`, on the line or straight above it."""
    hits = _waits_on_faked_requests(path.read_text(encoding="utf-8"))
    assert not hits, (
        f"{path.name}: waits for a request the module fakes at line(s) {hits}, which returns "
        "before the fake has answered it. Wait with expect_response, or comment why not."
    )


def test_the_faked_request_check_sees_what_it_is_for():
    fixture = (
        "PATH = '**/api/ingest/jobs/path'\n"
        "def _posted(name):\n"
        "    return lambda r: r.url.endswith('/api/ingest/jobs/path') and r.post_data_json['name'] == name\n"
        "def fake(page):\n"
        "    page.route(PATH, lambda route: route.fulfill(status=200, body='{}'))\n"
    )
    assert _waits_on_faked_requests(fixture + (
        "def t(page):\n"
        "    with page.expect_request(PATH):\n"                   # line 7
        "        pass\n"
        "    with page.expect_request(_posted('a.csv')):\n"       # line 9
        "        pass\n"
        "    with page.expect_request('**/api/ingest/preview'):\n"
        "        pass\n"
    )) == [7, 9]
    assert _waits_on_faked_requests(
        "def t(page, fake_route):\n"
        "    with fake_route(page, '/api/upload', lambda r: r.fulfill(status=507)):\n"
        "        with page.expect_request(lambda r: r.url.endswith('/api/upload')):\n"
        "            pass\n"
    ) == [3]
    assert _waits_on_faked_requests(fixture + (
        "def t(page):\n"
        "    with page.expect_response(PATH):\n"
        "        pass\n"
        "    # expect_request: held by the route, answered by hand below\n"
        "    with page.expect_request(PATH):\n"
        "        pass\n"
        "    with page.expect_request(PATH):   # expect_request: held too\n"
        "        pass\n"
    )) == []
    # A comment that is only nearby excuses nothing.
    assert _waits_on_faked_requests(fixture + (
        "def t(page):\n"
        "    # Both imports, in queue order.\n"
        "    with page.expect_request(PATH):\n"                  # line 8
        "        pass\n"
    )) == [8]
    # A module whose routes only hold or pass requests on fakes nothing.
    assert _waits_on_faked_requests(
        "def t(page):\n"
        "    page.route('**/api/x', lambda route: held.append(route))\n"
        "    with page.expect_request('**/api/x'):\n"
        "        pass\n"
    ) == []


# A file's name taken in page JS by splitting its path on "/" alone.
_SLASH_BASENAME = re.compile(r"""\.split\(\s*(['"])/\1\s*\)\s*\.(?:pop\(\s*\)|at\(\s*-1\s*\))""")


def _slash_basenames(src: str) -> list[int]:
    return [src[:m.start()].count("\n") + 1 for m in _SLASH_BASENAME.finditer(src)]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_file_name_from_splitting_a_path_on_slash(path):
    """`str(tmp_path / "x.log")` has backslashes on Windows, so page JS
    that names a file with `p.split('/').pop()` gets the whole path back
    there and the right name everywhere else. test_custom_delimiter_ui.py
    did that: its table was named after the full path, and it failed
    every night the nightly's Windows browser job drew its shard, which no
    Linux job could see. Take the name in Python (`p.name`) and hand the
    page both."""
    hits = _slash_basenames(path.read_text(encoding="utf-8"))
    assert not hits, (
        f"{path.name}: takes a file's name by splitting a path on '/' at line(s) {hits}, "
        "which is the whole path on Windows. Pass the name from Python (Path.name)."
    )


def test_the_slash_basename_check_sees_what_it_is_for():
    assert _slash_basenames("x\nnames = paths.map((p) => p.split('/').pop())\n") == [2]
    assert _slash_basenames('p.split("/").at(-1)') == [1]
    assert _slash_basenames("rel.split('/'); s.split(',').pop()") == []


def test_module_basenames_are_unique_across_tests_and_ui():
    """pytest imports test modules by basename (no __init__.py here), so
    tests/test_x.py and tests/ui/test_x.py collide: the second one to be
    collected errors with "import file mismatch" and interrupts the whole
    run — a backend-only run (`-m "not ui"`) included, since collection
    happens before marker selection. Happened twice; now it fails here,
    with names."""
    tests = Path(__file__).resolve().parent
    seen = {}
    dupes = []
    for path in sorted(tests.rglob("test_*.py")):
        if seen.setdefault(path.name, path) != path:
            dupes.append(f"{path.name}: {seen[path.name].relative_to(tests)} and {path.relative_to(tests)}")
    assert not dupes, "test module basenames must be unique across tests/ and tests/ui/:\n" + "\n".join(dupes)
