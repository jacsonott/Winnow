"""Traps that make a UI test pass without testing anything.

A UI test that silently does nothing is worse than no test: it reports
green, and the thing it was written to catch keeps happening. These are
checked statically because both traps look completely reasonable on the
page — they read as careful waiting.
"""

from __future__ import annotations

import ast
import re
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


def _slash_paths(src: str) -> list[int]:
    """Lines where a test's Python joins a filesystem path to more with
    "/" in a string (`f"{tmp_path}/acme"`), or takes one apart on "/"
    (`cur["path"].rsplit("/", 1)`).

    A path is `tmp_path`, `Path(...)`, `tmp_path_factory.mktemp(...)`,
    one of those `/ "more"` or `.parent`, `str()` of one, or a name
    assigned any of them. A split counts when the thing split says "path"
    and not "url", since a URL is split on "/" everywhere."""
    tree = ast.parse(src)
    paths = {"tmp_path"}

    def is_path(node) -> bool:
        if isinstance(node, ast.Name):
            return node.id in paths
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return is_path(node.left)
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            return is_path(node.value)
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in ("Path", "mktemp"):
                return True
            return name in ("str", "fspath") and len(node.args) == 1 and is_path(node.args[0])
        return False

    pairs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    pairs += zip(t.elts, node.value.elts)
                else:
                    pairs.append((t, node.value))
    grew = True
    while grew:
        grew = False
        for target, value in pairs:
            if isinstance(target, ast.Name) and target.id not in paths and is_path(value):
                paths.add(target.id)
                grew = True

    hits = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for a, b in zip(node.values, node.values[1:]):
                if (isinstance(a, ast.FormattedValue) and is_path(a.value)
                        and isinstance(b, ast.Constant) and str(b.value).startswith("/")):
                    hits.add(node.lineno)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in ("split", "rsplit") and node.args
              and isinstance(node.args[0], ast.Constant) and node.args[0].value == "/"):
            said = (ast.get_source_segment(src, node.func.value) or "").lower()
            if is_path(node.func.value) or ("path" in said and "url" not in said):
                hits.add(node.lineno)
    return sorted(hits)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_path_joined_or_split_on_slash(path):
    """The page writes a path with the separator its folder uses
    (home.js joinPath), and the server answers in the OS's own shape, so
    on Windows both are backslashes. test_group_folder_ui.py expected
    `f"{folder}/ui-gf-dc01.db-winnow"` and found the folder of a case with
    `rsplit("/", 1)`: four of its tests passed on every Linux job and
    failed the night the nightly's Windows browser job drew their shard
    (2026-11-20). Build and take apart paths with pathlib or os.path."""
    hits = _slash_paths(path.read_text(encoding="utf-8"))
    assert not hits, (
        f"{path.name}: joins or splits a filesystem path on '/' at line(s) {hits}, "
        "which Windows writes with backslashes. Use pathlib (folder / name, .name, .parent) "
        "or os.path (join, dirname, basename)."
    )


def test_the_slash_path_check_sees_what_it_is_for():
    # What test_group_folder_ui.py did before 2026-11-21.
    assert _slash_paths(
        "def t(page, tmp_path, cur):\n"
        "    folder = tmp_path / 'acme engagement'\n"
        "    share, third = tmp_path / 'share', tmp_path / 'third'\n"
        "    assert v == f'{folder}/ui-gf-dc01.db-winnow'\n"
        "    page.fill(f'{third}/x.db-winnow')\n"
        "    assert v == f'{tmp_path}/acme/x'\n"
        "    d = page.locator('.nc-path').input_value().rsplit('/', 1)[0]\n"
        "    d = cur['path'].rsplit('/', 1)[0]\n"
        "    leaf = str(share).split('/')[-1]\n"
    ) == [4, 5, 6, 7, 8, 9]
    # Not paths: a URL, a label with a slash in it, a path written the right way.
    assert _slash_paths(
        "def t(page, tmp_path, r, style, theme):\n"
        "    folder = tmp_path / 'acme'\n"
        "    name = r.url.rsplit('/', 1)[1]\n"
        "    seg = urlparse(r.url).path.split('/')\n"
        "    msg = f'{style}/{theme} still stripes'\n"
        "    api(f'/api/cases/{rec}/x')\n"
        "    assert v == os.path.join(str(folder), 'x') and v == str(folder / 'x')\n"
    ) == []


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
