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

Imports nothing. Anything that needs a path can depend on this without
worrying about what it drags in.
"""

from __future__ import annotations

from pathlib import Path

# The directory holding server.py — resolved, so a symlinked install
# compares equal to itself however it was reached.
#
# THIS MODULE LIVES IN winnow/, ONE LEVEL DOWN from the install root, so
# the root is this file's grandparent. That `.parent.parent` is the only
# line in the codebase that knows the layout; test_paths.py fails loudly
# if it ever stops matching where things actually are.
INSTALL_ROOT = Path(__file__).resolve().parent.parent
