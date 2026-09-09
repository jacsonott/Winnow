#!/usr/bin/env bash
# Double-click launcher for Linux. Finds Python and starts Winnow from the
# install root (the directory above this one); that opens the app window,
# same as running `python server.py` there yourself. No build step — this
# just locates the interpreter and hands off.
cd "$(dirname "$0")/.." || exit 1
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1; then exec "$py" server.py "$@"; fi
done
echo "Python 3 was not found on PATH. Install Python 3, then run: python3 server.py" >&2
read -r -p "Press Enter to close..." _
