"""The in-app log: a bounded ring the UI shows from Case ▾ → Log.

Errors used to go only to the terminal Winnow was started from, which an
analyst rarely has in front of them; server.py grew a ring for them. It
lives here now so the store can write to it too — an import that fails
inside a job thread, or finishes, is the thing the analyst most wants to
see and never could, and store.py cannot import server.py.

Stdlib only, imports nothing from the app. Three levels: "error" lights
the badge on the Case button, "warn" and "info" don't — the badge means
"something went wrong", and an import finishing must not cry wolf.
`error_seq` is the seq of the latest error, which is what the client
compares its last-seen mark against. Every record also prints to stderr,
so anyone watching the terminal sees what they always did.
"""

from __future__ import annotations

import collections
import datetime
import sys
import threading

LEVELS = ("error", "warn", "info")
RING = 2000

_ring: "collections.deque[dict]" = collections.deque(maxlen=RING)
_lock = threading.Lock()
_seq = 0
_error_seq = 0


def _normalize(level: str) -> str:
    lv = str(level or "").strip().lower()
    if lv == "warning":
        lv = "warn"
    return lv if lv in LEVELS else "info"


def record(level: str, message) -> int:
    """Append one entry; returns its seq."""
    global _seq, _error_seq
    lv = _normalize(level)
    with _lock:
        _seq += 1
        if lv == "error":
            _error_seq = _seq
        _ring.append({
            "seq": _seq,
            "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": lv,
            "message": str(message),
        })
        seq = _seq
    print(f"[{lv}] {message}", file=sys.stderr, flush=True)
    return seq


def entries() -> list[dict]:
    with _lock:
        return list(_ring)


def seq() -> int:
    with _lock:
        return _seq


def error_seq() -> int:
    with _lock:
        return _error_seq


def snapshot() -> dict:
    """What /api/log returns: the ring, the high-water seq, and the seq of
    the latest error (0 if none)."""
    with _lock:
        return {"entries": list(_ring), "seq": _seq, "error_seq": _error_seq}


def reset() -> None:
    """Tests only."""
    global _seq, _error_seq
    with _lock:
        _ring.clear()
        _seq = 0
        _error_seq = 0
