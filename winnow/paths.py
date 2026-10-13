"""Where this Winnow install lives on disk — decided once, here.

Three separate places used to work it out from their own location
(`Path(__file__).parent`), which is correct only for as long as every one
of them sits in the install's root directory. Two of those three are
load-bearing in a way that makes a silent wrong answer expensive:

- `workspace.WORKSPACE_DIR` is the analyst's case registry, saved
  filters, tag template and cases_dir. Resolve it a directory too deep
  and an existing install doesn't lose that state, it just stops finding
  it — which looks exactly like data loss.
- `updater.HERE` IS the install root: what an update backs up, what it
  replaces, and what `PROTECTED` (workspace/, plugins/, sessions/,
  cases/) is relative to. Pointed at the wrong directory it protects
  nothing and rewrites the wrong tree.

So the rule is one line of code knows the answer and everything else asks
it. If this module ever moves out of the root, this is the only
definition that changes — and tests/test_paths.py fails loudly if it
moves without being updated.

Imports nothing from the app. Anything that needs a path can depend on
this without worrying about what it drags in.

It also owns the one other path decision that is silent and expensive
when each caller makes it for itself: how to hand SQLite a path to open
read-only (`sqlite_ro_uri`, below).
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

# The directory holding server.py — resolved, so a symlinked install
# compares equal to itself however it was reached.
#
# THIS MODULE LIVES IN winnow/, ONE LEVEL DOWN from the install root, so
# the root is this file's grandparent. That `.parent.parent` is the only
# line in the codebase that knows the layout; test_paths.py fails loudly
# if it ever stops matching where things actually are.
INSTALL_ROOT = Path(__file__).resolve().parent.parent


def sqlite_ro_uri(path: str | os.PathLike, _path=os.path) -> str:
    """The `file:` URI that opens `path` read-only — for
    `sqlite3.connect(..., uri=True)` and for an ATTACH on a connection
    opened that way.

    Every read-only open in Winnow goes through here because pasting the
    path in between `file:` and `?mode=ro` is wrong in a way that never
    shows on a developer's machine: in a URI `#` starts a fragment, `?` starts
    the query and `%` starts an escape. A case kept under `Case #5/` was
    opened as `Case ` — read-WRITE, because `?mode=ro` went with the cut-off
    fragment — which created an empty file of that name and made every
    read fail with "no such table: sources"; previewing `History#1.db`
    created an empty `History` in the evidence folder and listed no
    tables. So the path is percent-encoded here, and only the query is
    literal.

    Absolute first (a relative URI would resolve against whatever the cwd
    is when SQLite opens it) — abspath, not resolve(): resolve() turns a
    mapped network drive into a UNC path on Windows, which is a different
    spelling of the file than the one the analyst gave and the one the
    case lock is keyed on. Then always `file://` + an empty authority +
    an absolute path, which is the one shape SQLite reads the same way on
    every platform:

        /home/a/Case #5/x.db     file:///home/a/Case%20%235/x.db?mode=ro
        C:\\Cases\\x.db           file:///C:/Cases/x.db?mode=ro
        \\\\server\\share\\x.db    file:////server/share/x.db?mode=ro

    The UNC form is why this is not `Path.as_uri()`: that writes
    `file://server/share/...`, and SQLite refuses any authority but
    `localhost` ("invalid uri authority"), so a case on a share would stop
    opening. Four slashes is the spelling SQLite documents for UNC.

    `_path` is for tests only: ntpath on Linux, to pin the Windows shapes
    on a machine that cannot open them."""
    p = _path.abspath(os.fspath(path))
    if _path.sep != "/":
        p = p.replace(_path.sep, "/")
    if not p.startswith("/"):          # a Windows drive path, "C:/..."
        p = "/" + p
    return "file://" + quote(p, safe="/:") + "?mode=ro"
