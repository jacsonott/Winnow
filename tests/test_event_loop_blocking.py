"""No route may do blocking work on the event loop.

FastAPI runs a plain `def` route in a worker thread and an `async def`
route ON THE EVENT LOOP. So an async route that calls something slow —
a Store method, an archive expansion, a plugin import — freezes the whole
server for its duration: every other request, the presence stream, the
grid the analyst is scrolling.

This has bitten twice. The plugin dispatcher called plugin handlers
directly, so one LLM call froze Winnow for as long as it took. Then
/api/ingest/upload called ingest_csv the same way: a 130MB CSV made a
trivial /api/version take 2.4 seconds (21ms after the fix).

Routes have to be `async def` when they touch `await file.read()` or the
request body, which is exactly where the heavy work tends to be — so the
rule is checked here rather than left to review. The fix is always the
same shape:

    return await run_in_threadpool(store().ingest_csv, tmp, name=...)

which is why passing `store().method` as an ARGUMENT is allowed while
CALLING it is not.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"

# Called directly, each of these can hold the loop for seconds or longer.
HEAVY_FUNCTIONS = {
    "_reload_plugins",      # imports every enabled plugin: arbitrary module-level Python
    "_ingest_via_plugin",   # runs a plugin parser
    "expand_archive",       # unpacks a whole archive
    "rmtree", "copytree", "move",
}

# Shutdown only — there is nothing left to serve by then.
EXEMPT_FUNCTIONS = {"_lifespan"}


def _is_route(fn: ast.AsyncFunctionDef) -> bool:
    return any(ast.unparse(d).startswith(("app.", "app.api_route"))
               for d in fn.decorator_list)


def _store_expr(node: ast.AST) -> bool:
    """`store()` or `STORE` — the handle every case operation hangs off."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "store":
        return True
    return isinstance(node, ast.Name) and node.id == "STORE"


def _offenders(fn: ast.AsyncFunctionDef) -> list[str]:
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        # store().anything(...) / STORE.anything(...) — a Store method CALL.
        # `run_in_threadpool(store().method, ...)` passes an attribute and is
        # not a call here, which is what makes the fixed form pass.
        if isinstance(f, ast.Attribute) and _store_expr(f.value):
            out.append(f"line {node.lineno}: store().{f.attr}() on the event loop")
        name = getattr(f, "attr", None) or getattr(f, "id", None)
        if name in HEAVY_FUNCTIONS:
            out.append(f"line {node.lineno}: {name}() on the event loop")
    return out


def _async_routes():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name not in EXEMPT_FUNCTIONS]


def test_the_scan_actually_sees_the_routes():
    """A rule that matches nothing passes forever."""
    routes = [fn for fn in _async_routes() if _is_route(fn)]
    assert len(routes) >= 15, f"only found {len(routes)} async routes — has the decorator shape changed?"
    assert any(fn.name == "api_ingest_upload" for fn in routes)


@pytest.mark.parametrize("fn", _async_routes(), ids=lambda fn: fn.name)
def test_no_async_handler_blocks_the_event_loop(fn):
    bad = _offenders(fn)
    assert not bad, (
        f"{fn.name} (server.py:{fn.lineno}) does blocking work on the event loop:\n  "
        + "\n  ".join(bad)
        + "\n\nWrap it: `await run_in_threadpool(store().method, args...)` — a plain "
          "`def` route gets this from FastAPI, an `async def` one must ask."
    )


def test_the_rule_would_catch_a_regression():
    """Guard the guard: the check must fail on the shape it exists for."""
    tree = ast.parse(
        "async def api_x():\n"
        "    return store().ingest_csv('/tmp/x')\n"
    )
    (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
    assert _offenders(fn), "the scan no longer catches a direct Store call"

    ok = ast.parse(
        "async def api_y():\n"
        "    return await run_in_threadpool(store().ingest_csv, '/tmp/x')\n"
    )
    (fn_ok,) = [n for n in ast.walk(ok) if isinstance(n, ast.AsyncFunctionDef)]
    assert not _offenders(fn_ok), "the threadpooled form must pass"
