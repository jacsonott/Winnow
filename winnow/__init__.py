"""Winnow's application code.

The root of the install holds only what an analyst runs — `server.py` and
`update.py` — plus the data directories that belong to them (`static/`,
`plugins/`, `workspace/`, `cases/`). Everything the app is made of lives
here.

`paths.INSTALL_ROOT` is the one place that knows where the root is
relative to this package; see its module docstring before moving anything.
"""
