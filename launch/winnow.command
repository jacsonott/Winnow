#!/usr/bin/env bash
# Double-click launcher for macOS (Finder runs a .command in Terminal).
# Starts Winnow from the install root — the directory above this one.
cd "$(dirname "$0")/.." || exit 1
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1; then exec "$py" server.py "$@"; fi
done
echo "Python 3 was not found on PATH. Install Python 3, then run: python3 server.py" >&2
read -r -p "Press Enter to close..." _
