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


def sqlite_rw_uri(path: str | os.PathLike) -> str:
    """sqlite_ro_uri's spelling of `path`, opened read-write. `mode=rw`
    never creates: a path that names nothing fails to open, where a plain
    `sqlite3.connect(path)` makes an empty database there."""
    return sqlite_ro_uri(path)[: -len("ro")] + "rw"


def sqlite_header_is_wal(path: str | os.PathLike) -> bool:
    """Whether the file's header says WAL mode (bytes 18-19 are 2) — read
    from the file itself, without opening it as a database."""
    try:
        with open(path, "rb") as f:
            head = f.read(20)
    except OSError:
        return False
    return len(head) == 20 and head.startswith(b"SQLite format 3\x00") and head[18] == 2


def sqlite_peek_uri(path: str | os.PathLike) -> str:
    """sqlite_ro_uri for a short read of a database this process doesn't
    have open — another case on the home screen, in a picker or a
    cross-case search — that leaves nothing beside it.

    `mode=ro` alone does leave something beside a WAL database, and every
    case is one: opening it creates the `-shm`, and a `-wal` when there
    isn't one, and a read-only connection can never remove them again.
    SQLite only does that on the last close of a connection that can
    write. So listing the recent cases on the home screen left a
    -wal/-shm pair beside every case in the list, for good.

    No `-wal` beside the file means no connection has it open (SQLite
    makes the -wal on the first read and removes it on the last close),
    so nothing is waiting in one to be checkpointed: the file as it stands
    is all of it, and `immutable=1` reads exactly that, creating nothing.
    With a -wal there, rows may live only in it, so this is plain
    `mode=ro`, which reads them; the -wal is already there, and the -shm
    almost always is.

    The -wal is looked for under the real path as well as the given one,
    and the file is opened by whichever spelling has it: off Windows SQLite
    names the -wal after the file a symlink points at, and on Windows after
    the path it was opened by. Opening a link whose -wal sits beside the
    target read a live case without the rows only its -wal has yet — on
    Windows, a case reopened through a link was "not a Winnow case file".

    For a read that is over within a request, not a connection kept
    around: immutable tells SQLite nobody else will change the file, so it
    takes no locks, and a Winnow that opened the case and checkpointed
    into it mid-read could hand this connection a mix of old and new
    pages. A checkpoint needs ~64 MB of writes (1000 of the case's 64 KB
    pages) or that Winnow closing the case again, which a look like this
    is long over before. A rollback-journal database creates nothing
    under `mode=ro` and gets it unchanged."""
    p = os.fspath(path)
    if not sqlite_header_is_wal(p):
        return sqlite_ro_uri(p)
    for q in (p, os.path.realpath(p)):
        if os.path.exists(q + "-wal"):
            return sqlite_ro_uri(q)
    return sqlite_ro_uri(p) + "&immutable=1"
