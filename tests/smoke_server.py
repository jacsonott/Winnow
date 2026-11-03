"""Start Winnow the way an analyst does and check that it answers.

    python tests/smoke_server.py [--root DIR] [--python EXE] [--port N]

Runs `server.py --case <new case> --open <ten-row CSV>` from --root (the
checkout by default) with --python (this interpreter by default), then
waits for /api/sources to list the CSV's ten rows and for the page and its
entry module and stylesheet to be served. Exits non-zero, saying which of
those it never saw, otherwise.

CI runs it twice. The Windows backend job runs it on the checkout, because
that is the platform analysts start Winnow on. The shipped-archive job runs
it on `git archive` output installed into a fresh virtualenv from
requirements.txt alone, because that is what an analyst downloads: a
runtime import of something only requirements-dev.txt installs, or a file
the app needs marked export-ignore, passes every test and fails there.

Not a test module (no test_ prefix), so pytest does not collect it, and
under tests/, so it is export-ignored like the rest of the suite.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROWS = 10


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="the install root to start server.py from")
    ap.add_argument("--python", default=sys.executable, help="the interpreter to start it with")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="winnow-smoke-")
    csv = os.path.join(tmp, "smoke.csv")
    with open(csv, "w", encoding="utf-8") as f:
        f.write("Timestamp,EventId\n" + "".join(f"2026-01-01 00:00:{i:02d},{4624 + i}\n" for i in range(ROWS)))
    base = f"http://127.0.0.1:{args.port}"
    server = subprocess.Popen([args.python, "server.py", "--no-browser", "--port", str(args.port),
                               "--case", os.path.join(tmp, "smoke.db-winnow"), "--open", csv],
                              cwd=args.root)
    try:
        deadline = time.monotonic() + args.timeout
        while True:
            if server.poll() is not None:
                print(f"server.py exited with {server.returncode} before answering", file=sys.stderr)
                return 1
            try:
                status, body = _get(f"{base}/api/sources")
                sources = json.loads(body) if status == 200 else []
                if any(s.get("row_count") == ROWS for s in sources):
                    print("sources:", [(s["name"], s["row_count"]) for s in sources])
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                print(f"server.py never listed smoke.csv's {ROWS} rows on /api/sources", file=sys.stderr)
                return 1
            time.sleep(1)
        missing = [path for path in ("/", "/static/js/main.js", "/static/style.css")
                   if _get(base + path)[0] != 200]
        if missing:
            print(f"served the API but not {', '.join(missing)}", file=sys.stderr)
            return 1
        print("smoke OK: the case opened, the CSV's rows are listed, the page is served")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
