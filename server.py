"""Winnow — a local web app for reading very large CSVs out of SQLite.

    python server.py --case case.db-winnow --open sample.csv

Then browse to http://127.0.0.1:8777
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import heapq
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from winnow import assoc as file_assoc
from winnow import browser
from winnow import instances
from winnow import paths
from winnow import plugin_api
from winnow import updater
from winnow import version
from winnow import workspace as WS
from winnow.store import (CASE_SUFFIX, SQLITE_IMPORT_EXTENSIONS, XLSX_IMPORT_EXTENSIONS, OpCancelled, Store,
                   describe_case_lock, probe_case_lock, sweep_orphan_views)

HERE = paths.INSTALL_ROOT  # static/, plugins/, examples/plugins/ all hang off the install root


# ------------------------------------------------------------ idle shutdown
#
# A server whose browser windows are all gone shuts itself down. Two things
# made this necessary at once: file associations spawn extra instances on
# ephemeral ports (see winnow/instances.py), which nobody will ever find a
# terminal for, and the long-standing failure mode the ⏻ button exists for —
# a forgotten server holding a case lock all weekend — stops requiring the
# analyst to remember anything.
#
# "A browser is connected" cannot be inferred from request traffic: an
# analyst reading the grid makes no requests at all, sometimes for hours.
# So every page holds open an EventSource to /api/presence, and the signal
# is the count of live streams — connection-oriented, immune to background-
# tab timer throttling, and torn down by the browser the moment a window
# closes. Zero streams sustained past a grace period, with nothing else
# going on, means nobody is here.
#
# Three holds keep this from ever destroying work:
#  - any live stream (the grace period also covers a reload's brief dip
#    to zero);
#  - in-flight HTTP (a multi-minute CSV export keeps downloading after its
#    tab closes — that connection counts until it finishes);
#  - running or queued ingest jobs (auto-exit closing the store would drop
#    a partial import, which is exactly what close() is documented to do).
#
# A server nothing ever connected to gets a much longer fuse: --no-browser
# plus a manual visit is a legitimate flow, and so is a launcher whose
# browser failed to open — the long fuse is what still reaps that one.

IDLE_EXIT_S = float(os.environ.get("WINNOW_IDLE_EXIT_S", "120"))
NEVER_CONNECTED_EXIT_S = float(os.environ.get("WINNOW_NEVER_CONNECTED_EXIT_S", "900"))
IDLE_TICK_S = float(os.environ.get("WINNOW_IDLE_TICK_S", "10"))


class _Presence:
    def __init__(self):
        self.streams = 0            # open /api/presence connections
        self.inflight = 0           # other HTTP requests mid-flight
        self.ever_connected = False
        self.started = time.monotonic()
        self.last_zero = time.monotonic()
        self.enabled = True         # main() clears this for --no-idle-shutdown


PRESENCE = _Presence()


def _jobs_running() -> bool:
    if STORE is None or STORE.closed:
        return False
    try:
        return any(j["status"] in ("running", "queued") for j in STORE.list_ingest_jobs())
    except Exception:  # noqa: BLE001 — a store mid-close must read as "not busy"
        return False


def _idle_exit_reason(now: float, p: _Presence, busy: bool) -> str | None:
    """Why this server should exit right now, or None. Pure on its inputs —
    the monitor passes live state, the tests pass constructed ones."""
    if not p.enabled or p.streams > 0 or busy:
        return None
    if p.ever_connected:
        idle = now - p.last_zero
        if idle >= IDLE_EXIT_S:
            return (f"no browser has been connected for {int(idle)}s — "
                    "shutting down (disable with --no-idle-shutdown)")
    else:
        up = now - p.started
        if up >= NEVER_CONNECTED_EXIT_S:
            return (f"no browser ever connected in {int(up)}s — "
                    "shutting down (disable with --no-idle-shutdown)")
    return None


async def _idle_monitor():
    while True:
        await asyncio.sleep(IDLE_TICK_S)
        reason = _idle_exit_reason(time.monotonic(), PRESENCE,
                                   PRESENCE.inflight > 0 or _jobs_running())
        if reason:
            print(reason, flush=True)
            _trigger_shutdown()
            return


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Startup/shutdown for the views scratch databases (invariant #3).

    Both halves exist for the same bug: those files are created per open
    case and deleted in Store.close(), and *nothing used to call close() on
    the way out*. main() blocks in uvicorn.run and the only other caller is
    a case switch, so Ctrl+C, a closed terminal, SIGTERM and every crash
    left a `winnow-views-*.db` (+ -wal/-shm) behind for good — in /dev/shm
    on Linux, in the platform tempdir (`C:\\Windows\\Temp` for an account
    with no TMP/TEMP) on Windows, at whatever size that session's largest
    materialised view reached.

    Shutdown closing the store fixes every *clean* exit, including Ctrl+C —
    uvicorn runs the lifespan for SIGINT/SIGTERM — and it's the only half
    that can be exact. Startup sweeping is what covers the rest (SIGKILL,
    OOM, power loss) plus whatever backlog a machine already has; it can't
    hurt a second live Winnow, see sweep_orphan_views. Attached here rather
    than in main() so `uvicorn server:app` gets both too."""
    global STORE
    swept = sweep_orphan_views()
    with contextlib.suppress(Exception):
        gone = _sweep_quicklook()
        if gone:
            print(f"Cleaned up {gone} abandoned quick-look case(s)")
    if swept["removed"]:
        print(f"Cleaned up {swept['removed']} orphaned temp file(s) from previous runs "
              f"({swept['bytes_freed'] / (1 << 20):.1f} MB)")
    monitor = asyncio.create_task(_idle_monitor())
    try:
        yield
    finally:
        monitor.cancel()
        # Take this server out of the instance registry FIRST, so a launcher
        # racing our shutdown doesn't pick a server that is mid-teardown.
        with contextlib.suppress(Exception):
            instances.unregister()
        old, STORE = STORE, None
        if old is not None:
            # Also cancels and joins running ingest jobs (bounded by one
            # batch commit each), so Ctrl+C mid-import drops its partial
            # source instead of stranding a half-table that looks complete.
            with contextlib.suppress(Exception):
                old.close()


app = FastAPI(title="Winnow", lifespan=_lifespan)
STORE: Store | None = None

# ---------------------------------------------------------------- plugins

# Loaded once at import (so `uvicorn server:app` gets them too, not just
# `python server.py`) from plugins/ next to server.py plus an optional
# WINNOW_PLUGINS_DIR env var; main() adds any --plugins-dir flags on top.
# A plugin that fails to load is recorded with its error and skipped — it
# shows up in GET /api/plugins with the reason, and never takes the server
# down. See plugin_api.py for the whole model (and its security note: a
# plugin is arbitrary local Python, same trust model as a Notepad++
# plugin — the analyst putting it in plugins/ is the consent step).
PLUGINS = plugin_api.PluginRegistry()


BUNDLED_PLUGIN_DIR = HERE / "examples" / "plugins"


def _plugin_dirs(extra: list[str] | None = None) -> list[Path]:
    # plugins/ first: it's the install target (PLUGIN_DIRS[0]), and load()'s
    # first-directory-wins rule means an analyst's installed copy of an
    # example shadows the bundled one. The bundled dir makes the shipped
    # examples appear in Settings → Plugins with no install step — default
    # OFF (see PluginPrefs.enabled_bundled), so nothing runs unasked.
    dirs = [HERE / "plugins", BUNDLED_PLUGIN_DIR]
    env = os.environ.get("WINNOW_PLUGINS_DIR")
    if env:
        dirs.append(Path(env))
    dirs.extend(Path(p) for p in (extra or []))
    return dirs


# Module-level and mutable on purpose: main() swaps in a longer list when
# --plugins-dir flags are given, and tests point it at a tmp dir. The first
# entry is where /api/plugins/install copies new plugins to.
PLUGIN_DIRS: list[Path] = _plugin_dirs()


def _case_plugin_overrides() -> dict[str, bool]:
    """The open case's per-plugin overrides ({fs_name: bool}), or {} when no
    case is open. Stored in the case file (case_settings) rather than the
    workspace on purpose: "this case needs the pivot tab" is a statement
    about the investigation, and it should still be true when the case file
    is handed to another analyst."""
    if STORE is None or STORE.closed:
        return {}
    try:
        raw = STORE.get_case_settings().get("plugin_overrides")
        overrides = json.loads(raw) if raw else {}
        return {k: bool(v) for k, v in overrides.items()} if isinstance(overrides, dict) else {}
    except Exception:
        return {}


def _reload_plugins() -> None:
    """Rescan PLUGIN_DIRS under the effective enablement policy: the open
    case's override wins where set, else the machine default (installed
    plugins default on via the disabled list; bundled examples default off
    via the enabled list). Cheap enough (a directory listing plus importing
    whatever's enabled) that toggles, installs and case switches just call
    it — no server restart involved. Case switches MUST call it: "a
    disabled plugin's code never runs" is a per-case statement now."""
    overrides = _case_plugin_overrides()

    def enabled_for(fs_name: str, directory: str) -> bool:
        if fs_name in overrides:
            return overrides[fs_name]
        default_on = Path(directory) != BUNDLED_PLUGIN_DIR
        return WS.plugin_prefs.machine_enabled(fs_name, default_on)

    PLUGINS.load(PLUGIN_DIRS, enabled_for=enabled_for, bundled_dirs=[BUNDLED_PLUGIN_DIR])


_reload_plugins()

# ---------------------------------------------------------------------- csrf

# Winnow has no login/auth by design — it's a local, single-analyst
# tool (see CLAUDE.md). That means the only thing standing between a
# malicious web page (open in the same browser, on some other tab) and this
# server is the browser's cross-origin request rules. Plain JSON POSTs are
# already safe: fetch() with a JSON body forces a CORS preflight, and since
# this app sends no CORS headers, the browser blocks it. But a handful of
# routes take multipart/form-data (file uploads: CSV import, session import,
# saved-filters import) — that's a CORS-*simple* content type, so a
# cross-origin page can fire one at this server with zero preflight and no
# way to read the response, but every side effect (importing a fabricated
# CSV into whatever case happens to be open, tampering with session state)
# still happens. Requiring this header on every state-changing request closes
# that gap: a cross-origin page can't attach a custom header without
# triggering a preflight, and that preflight fails the same way a JSON
# POST's already does.
CSRF_HEADER = "X-Timeline-Lite-Client"


# A request that was already executing against the previous case's Store
# when a case switch closed it can't be salvaged — sqlite3 raises
# ProgrammingError("Cannot operate on a closed database") from whatever
# query it runs next. That's not a server bug, it's "the case you were
# talking to is gone": answer 409 with a message that says so, instead of
# letting it explode into a full 500 traceback in the console (seen in the
# wild with several browsers on one --host 0.0.0.0 server — one client
# switching cases while another was mid-request). Deliberately worded
# WITHOUT "expired": app.js's ensurePage auto-rebuilds on 409s whose
# message contains "expired", and a stale tab auto-rebuilding against the
# newly opened case could silently show the wrong case's data under the
# old tab's name. Any other ProgrammingError is a genuine bug and stays a
# 500.
@app.exception_handler(sqlite3.ProgrammingError)
async def closed_database_handler(request: Request, exc: sqlite3.ProgrammingError):
    # JSONResponse with a `detail` key, matching HTTPException's shape —
    # app.js's api() reads err.message from there.
    if "closed database" in str(exc).lower():
        return JSONResponse(
            {"detail": "The case this request targeted has been closed or switched — reload the page."},
            status_code=409,
        )
    return JSONResponse({"detail": f"Internal error: {exc}"}, status_code=500)


@app.exception_handler(OpCancelled)
async def op_cancelled_handler(request: Request, exc: OpCancelled):
    # 499 ("client closed request"): not an error and not the analyst's
    # fault — the frontend treats it as "keep what you had". Deliberately
    # not 4xx-per-endpoint: any cancellable operation can raise this.
    return JSONResponse({"detail": "Cancelled"}, status_code=499)


@app.middleware("http")
async def require_client_header(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
        if request.headers.get(CSRF_HEADER) != "1":
            return PlainTextResponse(
                "Missing required client header — this endpoint can't be called cross-origin.",
                status_code=403,
            )
    return await call_next(request)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    # Piggybacked in-flight counting for idle shutdown — this middleware
    # already wraps every request, so the marginal cost is two increments.
    # /api/presence is excluded: its response streams for the LIFETIME of a
    # page, so counting it would read as "busy" forever; it has its own
    # stream counter. A download that outlives its tab (a multi-minute CSV
    # export) counts here until the body finishes, which is the hold that
    # keeps auto-exit from killing it.
    counted = request.url.path != "/api/presence"
    if counted:
        PRESENCE.inflight += 1
    try:
        return await _no_cache_static_inner(request, call_next)
    finally:
        if counted:
            PRESENCE.inflight -= 1


async def _no_cache_static_inner(request: Request, call_next):
    """StaticFiles sends Last-Modified/ETag but no Cache-Control, which
    leaves the browser free to serve a stale index.html/app.js/style.css
    from its own disk cache for an unbounded time — even across a normal
    reload, only a hard refresh forces it to re-check. There's no build
    step or content-hashed filenames here (CLAUDE.md: airgapped, no build
    step) to cache-bust with, and this app's own static assets change
    often during active use — so force revalidation on every load instead.
    Still cheap: browsers get a 304 back from StaticFiles' own ETag/
    Last-Modified support when nothing actually changed, so this doesn't
    mean re-downloading the file every time, just always double-checking."""
    response = await call_next(request)
    # /plugin_assets/ gets the same treatment as /static/ for the same
    # reason — a plugin's tab JS changes during development, and a stale
    # cached module is the same class of confusing non-bug. (import() also
    # carries a ?v=<gen> cache-buster, but that only changes when the
    # registry reloads, not on every save while iterating on a tab.)
    if request.url.path.startswith(("/static/", "/plugin_assets/")) or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache"
    return response


def store() -> Store:
    if STORE is None:
        raise HTTPException(500, "No case file open")
    return STORE


def _default_tags_tuples() -> list[tuple]:
    return WS.tags.as_tuples()


def _open_store(path: str) -> Store:
    return Store(path, default_tags=_default_tags_tuples())


# ------------------------------------------------------------------ schemas

class ViewSpec(BaseModel):
    source_id: int
    filters: list[dict] = []
    sort: list[dict] = []
    search: str = ""
    search_mode: str = "contains"  # "contains" | "regex" | "advanced"
    search_terms: list[dict] = []  # advanced mode: [{term, connector: "AND"|"OR", exclude: bool}]
    filter_tree: dict | None = None  # guided filter-builder tree; group/cond/raw nodes
    tags: list = []
    time_range: dict | None = None  # {enabled, column, start, end} — see _compile_where; survives filter/preset changes
    op_token: str | None = None  # client-generated cancel handle — see Store.cancel_op


class TagWrite(BaseModel):
    source_id: int = 0
    rids: list[int] = []
    pairs: list[list[int]] | None = None  # [[source_id, rid], ...] — merged-view tagging, mixed real sources
    tag_id: int
    on: bool = True


class TagDef(BaseModel):
    id: int | None = None
    name: str
    color: str = "#8899aa"
    hotkey: str | None = None


class NoteWrite(BaseModel):
    source_id: int
    rid: int
    note: str = ""


class LayoutWrite(BaseModel):
    source_id: int
    payload: dict


class SavedViewWrite(BaseModel):
    source_id: int
    name: str
    payload: dict


class SqlQuery(BaseModel):
    sql: str
    limit: int = 5000


class IngestPath(BaseModel):
    path: str
    name: str | None = None
    delimiter: str | None = None
    build_fts: bool = True
    has_header: bool = True
    column_types: list[str] | None = None


class IngestJsonPath(BaseModel):
    path: str
    name: str | None = None
    flatten_mode: str = "none"
    flatten_depth: int = 0
    build_fts: bool = True


class IngestPluginPath(BaseModel):
    path: str
    format_id: str          # namespaced "<plugin>.<format>" — see GET /api/plugins
    name: str | None = None
    options: dict = {}      # values for the format's declared options
    build_fts: bool = True


class DirectoryScan(BaseModel):
    root: str
    recursive: bool = True
    extensions: list[str] | None = None
    include_patterns: list[str] = []
    exclude_patterns: list[str] = []
    filename_patterns: list[str] = []  # plugin formats' bare-name patterns ($MFT, $J) — see scan_import_directory


class ImportProfileWrite(BaseModel):
    id: int | None = None
    name: str
    extensions: list[str] | None = None
    include_patterns: list[str] = []
    exclude_patterns: list[str] = []
    recursive: bool = True


class CaseOpen(BaseModel):
    path: str
    force: bool = False  # open even though another Winnow holds it — see probe_case_lock


class CaseCreate(BaseModel):
    path: str
    name: str
    group: str = ""
    notes: str = ""


class CaseUpdate(BaseModel):
    name: str | None = None
    group: str | None = None
    notes: str | None = None


# ------------------------------------------------------------------- routes

def _case_display_name(path: str) -> str:
    """The name shown in the UI (brand button, home screen list) for a case
    file — whatever's registered in workspace/cases.json, falling back to
    the bare filename for the (normally unreachable) case where a Store got
    opened without ever going through CaseRegistry.create."""
    rec = WS.cases.find_by_path(path)
    return rec["name"] if rec else os.path.splitext(os.path.basename(path))[0]


@app.get("/api/case/current")
def api_case_current():
    if STORE is None:
        return {"open": False}
    return {"open": True, "path": STORE.path, "name": _case_display_name(STORE.path),
            "temp": _is_temp_case(STORE.path)}


# ------------------------------------------------------------ quick-look cases
#
# A file opened through an OS file association lands in a TEMPORARY case:
# the analyst is looking at one artifact, and making them name and file a
# case first is exactly the friction the association exists to remove. A
# temp case is an ordinary case file in <cases_dir>/quicklook/ — temp-ness
# is a fact about the path, not a flag anywhere, so it survives restarts
# and needs no state. It stays out of the home screen's registry until the
# analyst saves it; from the banner they can name it (the file moves out of
# quicklook/ and registers), push its tables into an existing case
# (copy_sources_to), or discard it.

def _cases_dir() -> str:
    configured = os.environ.get("WINNOW_CASES_DIR") or WS.machine_prefs.get("cases_dir")
    base = configured or str(paths.INSTALL_ROOT / "cases")
    return os.path.abspath(os.path.expanduser(base))


QUICKLOOK_DIRNAME = "quicklook"


def _is_temp_case(path: str) -> bool:
    return os.path.basename(os.path.dirname(os.path.abspath(path))) == QUICKLOOK_DIRNAME


def _new_temp_case_path() -> str:
    d = os.path.join(_cases_dir(), QUICKLOOK_DIRNAME)
    os.makedirs(d, exist_ok=True)
    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(d, f"quicklook-{stamp}{CASE_SUFFIX}")
    n = 1
    while os.path.exists(path):
        path = os.path.join(d, f"quicklook-{stamp}-{n}{CASE_SUFFIX}")
        n += 1
    return path


def is_winnow_case_file(path: str) -> bool:
    """Distinguish a Winnow case from a generic SQLite file — double-
    clicking a case should OPEN it, not ingest it as data. Read-only,
    tolerant of anything unreadable."""
    try:
        with open(path, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                return False
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        return {"sources", "tag_defs", "row_tags"} <= names
    except Exception:  # noqa: BLE001 — unreadable means "not a case"
        return False


def _sweep_quicklook(max_age_days: int = 7) -> int:
    """Delete abandoned quick-look cases: old, unlocked, and holding no
    analysis (no tags, no notes, no saved sessions). Anything with work in
    it is kept forever — a janitor that can eat findings is worse than the
    disk it saves."""
    d = os.path.join(_cases_dir(), QUICKLOOK_DIRNAME)
    if not os.path.isdir(d):
        return 0
    cutoff = __import__("time").time() - max_age_days * 86400
    removed = 0
    for fn in os.listdir(d):
        # New quick-looks are CASE_SUFFIX; ".db" keeps sweeping the ones a
        # pre-change build left in this directory.
        if not (fn.endswith(CASE_SUFFIX) or fn.endswith(".db")):
            continue
        path = os.path.join(d, fn)
        try:
            if os.path.getmtime(path) > cutoff or probe_case_lock(path):
                continue
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                busy = any(
                    conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("row_tags", "row_notes", "sessions")
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone())
            finally:
                conn.close()
            if busy:
                continue
            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(OSError):
                    os.remove(path + suffix)
            with contextlib.suppress(OSError):
                os.remove(path + ".winnow-lock")
            removed += 1
        except OSError:
            continue
    return removed


def _trigger_shutdown() -> None:
    """Deliver SIGINT to this process shortly after the response flushes.

    A separate function so tests can monkeypatch it instead of killing the
    pytest process. signal.raise_signal rather than os.kill because it also
    works on Windows (where SIGINT can't be delivered via kill); uvicorn
    treats it exactly like Ctrl+C — graceful shutdown, so _lifespan's
    Store.close() still runs and the views scratch files are deleted. The
    short delay is what lets the HTTP response reach the browser first."""
    import signal
    import threading
    threading.Timer(0.3, lambda: signal.raise_signal(signal.SIGINT)).start()


class UpdateApply(BaseModel):
    """Applying is a separate, explicit POST from checking — a check must
    never be able to install anything as a side effect."""
    confirm: bool = False


@app.get("/api/presence")
async def api_presence():
    """The connection every page holds open so the server knows a browser
    is attached. EventSource on the client, a comment-ping stream here —
    no data ever flows, the CONNECTION is the message. Disconnect is
    noticed at the next ping (≤15s), which is well inside the idle grace."""
    async def stream():
        PRESENCE.streams += 1
        PRESENCE.ever_connected = True
        try:
            while True:
                yield ": ping\n\n"
                await asyncio.sleep(15)
        finally:
            PRESENCE.streams -= 1
            if PRESENCE.streams == 0:
                PRESENCE.last_zero = time.monotonic()
    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/version")
def api_version():
    """What this install is. Cheap and unauthenticated on purpose: an
    analyst writing up a case needs to be able to state the tool version
    without going digging.

    `source` distinguishes a release from a develop-branch sync. It
    matters because version.py on develop still reads as the last
    released number while the code around it has moved on — so a beta
    tester who reported only "0.1.0" would be describing a build nobody
    can reproduce."""
    src = updater.installed_source()
    return {"version": version.VERSION, "source": src,
            "is_release": src == "release"}


@app.post("/api/updates/check")
def api_updates_check():
    """Ask GitHub whether there's a newer release. ONLY ever runs because
    the analyst clicked Check — Winnow has no background poll and nothing
    at startup. The box may be airgapped, and a forensic tool that phones
    home unasked is its own problem; the error path says how to update
    offline instead."""
    try:
        return updater.check_for_update(current=version.VERSION)
    except updater.UpdateError as e:
        raise HTTPException(400, str(e))


@app.post("/api/updates/apply")
def api_updates_apply(body: UpdateApply):
    """Download and install the latest release, backing up first.

    The server keeps running the OLD code afterwards — Python already
    imported it — so the response tells the UI to prompt for a restart
    rather than pretending the new version is live."""
    if not body.confirm:
        raise HTTPException(400, "Refusing to update without confirm=true")
    try:
        info = updater.check_for_update(current=version.VERSION)
        if not info["available"]:
            raise HTTPException(400, f"Already on the latest release ({info['latest']})")
        archive = updater.download(info["url"], Path(tempfile.gettempdir()))
        res = updater.apply_update(archive, updater.HERE)
        return {**res, "restart_required": True}
    except updater.UpdateError as e:
        raise HTTPException(400, str(e))


@app.post("/api/updates/rollback")
def api_updates_rollback():
    try:
        return {**updater.rollback(updater.HERE), "restart_required": True}
    except updater.UpdateError as e:
        raise HTTPException(400, str(e))


@app.post("/api/shutdown")
def api_shutdown():
    """The UI's off switch. Winnow is a local single-analyst tool whose
    server usually lives in a forgotten terminal (or was started by a
    shortcut with no terminal at all) — "close the browser tab" leaving the
    process running forever is how a case file stays locked all weekend.
    Nothing to save: every write already went to the case file."""
    _trigger_shutdown()
    return {"ok": True}


@app.post("/api/case/open")
def api_case_open(body: CaseOpen):
    global STORE
    if not os.path.isfile(body.path):
        raise HTTPException(400, f"No case file at {body.path}")
    # Re-opening the case that's already open is a no-op, not a reopen. Two
    # reasons, and the second is the load-bearing one: the client's own
    # state reset in openCase() is what that request is really for, and the
    # open-before-close ordering below would otherwise make this process
    # probe its *own* case lock and refuse itself.
    # `not STORE.closed` matters: STORE outlives close() on both the case-
    # switch path and the legacy-preset migration, and reopening a case
    # whose Store has already been closed is a real reopen, not a no-op.
    if STORE is not None and not STORE.closed and os.path.abspath(STORE.path) == os.path.abspath(body.path):
        rec = WS.cases.find_by_path(body.path) or WS.cases.create(
            body.path, name=os.path.splitext(os.path.basename(body.path))[0]
        )
        WS.cases.touch_opened(rec["id"])
        return {"sources": STORE.list_sources(), "name": rec["name"]}
    if not body.force:
        holder = probe_case_lock(body.path)
        if holder:
            # 409 with a structured detail the frontend turns into the
            # "open anyway / don't open" prompt. Advisory: re-POST with
            # force=true is the way through, and nothing here can refuse it.
            raise HTTPException(409, {
                "error": "case_in_use",
                "message": describe_case_lock(holder),
                "holder": holder,
            })
    # Open the new store BEFORE closing/replacing the old one. The old
    # order (close, then open) left a window where STORE pointed at a
    # closed connection — any request landing in it (or, if the open
    # failed, until the next successful open) died with "Cannot operate
    # on a closed database". With several browsers pointed at one server
    # (--host 0.0.0.0), one client switching cases while another browses
    # makes that window a matter of when, not if. Requests already in
    # flight against the old store when it closes are inherently
    # unsalvageable — those get a clean 409 from the
    # closed-database exception handler below instead of a 500 traceback.
    old = STORE
    try:
        STORE = _open_store(body.path)
    except Exception as e:
        raise HTTPException(400, f"Could not open case: {e}")
    if old is not None:
        try:
            old.close()
        except Exception:
            pass  # already closed / mid-request — nothing useful to do
    legacy_presets = STORE.pop_legacy_presets()
    if legacy_presets:
        WS.filters.import_all({"filters": legacy_presets}, merge=True)
    rec = WS.cases.find_by_path(body.path) or WS.cases.create(
        body.path, name=os.path.splitext(os.path.basename(body.path))[0]
    )
    WS.cases.touch_opened(rec["id"])
    # The instance registry is how an association-launch decides whether this
    # server is reusable; a case opening here is what makes it not-idle.
    with contextlib.suppress(Exception):
        instances.set_case(body.path)
    # The effective plugin set is per-case now (case_settings overrides), so
    # a case switch is a policy change: reload so an "on in this case" tab
    # exists the moment the case does, and an "off in this case" plugin's
    # code is unloaded rather than merely unlisted.
    _reload_plugins()
    return {"sources": STORE.list_sources(), "name": rec["name"]}


class CopySourcesBody(BaseModel):
    target_path: str
    source_ids: list[int]


@app.post("/api/case/copy_sources")
def api_case_copy_sources(body: CopySourcesBody):
    """Copy sources from the open case into another case file — the "save
    these quick-look tables into my real case" flow. A target open in
    another Winnow is refused with the holder named (409, same contract as
    opening a locked case)."""
    if _jobs_running():
        # A source mid-ingest has an accurate-but-growing row_count; the
        # ATTACH copy would snapshot whatever happened to be committed
        # and file it in the target as a complete table.
        raise HTTPException(409, "Still importing — copy again when the import finishes")
    try:
        return store().copy_sources_to(body.target_path, body.source_ids)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        code = 409 if "another Winnow" in str(e) else 400
        raise HTTPException(code, str(e))


class AssocOpenBody(BaseModel):
    files: list[str]


@app.post("/api/assoc/open")
def api_assoc_open(request: Request, body: AssocOpenBody):
    """A file-association launch landing on an already-running server.

    Loopback-only: it creates cases and ingests files by path, both
    write-shaped powers that stay local. Three states, three answers:
    no case open — create a fresh quick-look case and ingest into it; a
    QUICK-LOOK case open — add to it (five files double-clicked together
    arrive as five launcher invocations, and they belong in one case); a
    real case open — 409, the launcher spawns its own instance instead,
    because switching this server's case would yank every window the
    analyst has open."""
    global STORE
    if not _is_loopback(request):
        raise HTTPException(403, "Association opens are only accepted from this machine")
    files = [os.path.abspath(f) for f in body.files]
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        raise HTTPException(400, f"No file at {missing[0]}")

    if STORE is not None and not STORE.closed and not _is_temp_case(STORE.path):
        raise HTTPException(409, "A case is open here — spawn a fresh instance instead")
    if STORE is None or STORE.closed:
        temp = _new_temp_case_path()
        STORE = _open_store(temp)
        with contextlib.suppress(Exception):
            instances.set_case(temp)
        _reload_plugins()

    started, skipped = [], []
    for f in files:
        try:
            started.append(_assoc_ingest(f))
        except Exception as e:  # noqa: BLE001 — a corrupt .db raises
            # sqlite3.DatabaseError and a truncated .xlsx BadZipFile, not
            # ValueError; one unreadable file must skip, not 500 the open.
            skipped.append({"file": os.path.basename(f), "reason": str(e)})
    return {"case": STORE.path, "temp": True, "started": started, "skipped": skipped}


def _assoc_ingest(path: str) -> dict:
    """Queue one associated file into the open case, headless — nobody is
    standing at an import modal. SQLite files and workbooks normally get a
    picker; a quick-look takes EVERYTHING (all tables, all sheets), which
    is what "just show me the file" means."""
    kind = _ingest_kind_for_path(path)
    opts = {"build_fts": True}
    if kind == "sqlite":
        tables = store().preview_sqlite_tables(path)["tables"]
        if not tables:
            raise ValueError("no tables in this SQLite file")
        opts["tables"] = [{"table": t["name"],
                           "timestamp_columns": t["likely_timestamp_columns"]} for t in tables]
    elif kind == "xlsx":
        sheets = store().preview_xlsx_sheets(path)["tables"]
        keep = [t for t in sheets if t["row_count"] > 0]
        if not keep:
            raise ValueError("no data sheets in this workbook")
        opts["tables"] = [{"table": t["name"]} for t in keep]
    job = store().start_ingest_job(kind, path, options=opts)
    return {"file": os.path.basename(path), "job_id": job["job_id"], "kind": kind}


# ------------------------------------------------- association registration
#
# Settings → File associations (and the one-time after-import offer) talk
# to these. All loopback-only: they change per-user OS state, which a
# hostile page in another tab has no business touching — same reasoning
# as /api/assoc/open. The catalogue is winnow/assoc.py's builtins plus
# whatever extensions the currently loaded plugins claim, so a new ingest
# plugin shows up in the panel with no code change here.

_ASSOC_ASKED_KEY = "assoc_asked_exts"


class AssocExtsBody(BaseModel):
    exts: list[str]


def _assoc_catalogue() -> list[dict]:
    return file_assoc.supported_types(PLUGINS.list_formats())


def _assoc_pick(exts: list[str], catalogue: list[dict]) -> list[dict]:
    by_ext = {t["ext"]: t for t in catalogue}
    picked = []
    for e in exts:
        e = e.lower()
        if not e.startswith("."):
            e = "." + e
        t = by_ext.get(e)
        if t is None:
            raise HTTPException(400, f"Winnow has no importer for {e}")
        picked.append(t)
    return picked


def _assoc_adapter(request: Request):
    if not _is_loopback(request):
        raise HTTPException(403, "association changes are loopback-only")
    a = file_assoc.adapter()
    if a is None:
        raise HTTPException(400, "file associations aren't supported on this platform")
    return a


def _mark_asked(exts: list[str]) -> None:
    asked = set(WS.machine_prefs.get(_ASSOC_ASKED_KEY) or [])
    asked.update(e.lower() for e in exts)
    WS.machine_prefs.set(_ASSOC_ASKED_KEY, sorted(asked))


@app.get("/api/assoc/types")
def api_assoc_types(request: Request):
    if not _is_loopback(request):
        raise HTTPException(403, "association status is loopback-only")
    catalogue = _assoc_catalogue()
    a = file_assoc.adapter()
    st = a.status(catalogue) if a else {}
    asked = set(WS.machine_prefs.get(_ASSOC_ASKED_KEY) or [])
    return {"platform": file_assoc.platform_name(),
            "types": [{**t, **st.get(t["ext"], {"registered": False, "default": False}),
                       "asked": t["ext"] in asked} for t in catalogue]}


@app.post("/api/assoc/register")
def api_assoc_register(request: Request, body: AssocExtsBody):
    a = _assoc_adapter(request)
    catalogue = _assoc_catalogue()
    try:
        a.register(_assoc_pick(body.exts, catalogue), catalogue)
    except ValueError as e:   # a malformed mimeapps.list, with the fix named
        raise HTTPException(400, str(e))
    _mark_asked(body.exts)   # an explicit yes is also an answer
    return {"ok": True}


@app.post("/api/assoc/unregister")
def api_assoc_unregister(request: Request, body: AssocExtsBody):
    a = _assoc_adapter(request)
    catalogue = _assoc_catalogue()
    try:
        a.unregister(_assoc_pick(body.exts, catalogue), catalogue)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/assoc/default")
def api_assoc_default(request: Request, body: AssocExtsBody):
    a = _assoc_adapter(request)
    catalogue = _assoc_catalogue()
    picked = _assoc_pick(body.exts, catalogue)
    refused = [t["ext"] for t in picked if not t["default_ok"]]
    if refused:
        # Handler-only types have real owners (Excel, editors, DB tools);
        # the catalogue is the policy and the API enforces it — the UI
        # not offering the button is not enough.
        raise HTTPException(400, f"{', '.join(refused)} can only be a handler, not the default")
    try:
        result = a.make_default(picked, catalogue) or {}
    except ValueError as e:
        raise HTTPException(400, str(e))
    _mark_asked(body.exts)
    return {"ok": True, **result}


@app.post("/api/assoc/asked")
def api_assoc_asked(request: Request, body: AssocExtsBody):
    # "No" is an answer to remember too — the offer fires once per type.
    if not _is_loopback(request):
        raise HTTPException(403, "loopback-only")
    _assoc_pick(body.exts, _assoc_catalogue())
    _mark_asked(body.exts)
    return {"ok": True}


class CaseSaveAsBody(BaseModel):
    name: str


@app.post("/api/case/save_as")
def api_case_save_as(body: CaseSaveAsBody):
    """Promote the open quick-look case to a real one: move the file out of
    quicklook/, register it on the home screen under the given name.

    The close→rename→reopen dance is not optional: Windows refuses to
    rename a file with open handles, so the store must genuinely release
    it first. The window where STORE is closed is the same one every case
    switch already has — in-flight requests get the same 409."""
    global STORE
    if STORE is None or STORE.closed:
        raise HTTPException(400, "No case is open")
    if not _is_temp_case(STORE.path):
        raise HTTPException(400, "Only a quick-look case can be saved this way — "
                                 "this one already has a home")
    if _jobs_running():
        # Closing the store cancels running ingest jobs, and a cancelled
        # ingest DROPS its partial source (the cancel contract) — so
        # "save while the file is still importing" would quietly produce
        # an empty saved case. Natural timing, too: double-click a big
        # file, banner appears, analyst clicks Save immediately.
        raise HTTPException(409, "Still importing — save again when the import finishes")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Give the case a name")
    slug = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "case"
    dest = os.path.join(_cases_dir(), f"{slug}{CASE_SUFFIX}")
    if os.path.exists(dest):
        raise HTTPException(400, f"A case file named {os.path.basename(dest)} already exists")

    old_path = STORE.path
    STORE.close()
    try:
        os.replace(old_path, dest)
    except OSError as e:
        STORE = _open_store(old_path)   # failed rename leaves everything as it was
        raise HTTPException(400, f"Could not move the case file: {e}")
    for suffix in ("-wal", "-shm", ".winnow-lock"):
        with contextlib.suppress(OSError):
            os.remove(old_path + suffix)
    STORE = _open_store(dest)
    rec = WS.cases.create(dest, name=name)
    WS.cases.touch_opened(rec["id"])
    with contextlib.suppress(Exception):
        instances.set_case(dest)
    return {"path": dest, "name": name, "temp": False}


@app.post("/api/case/discard")
def api_case_discard():
    """Throw a quick-look case away. Refused for real cases — deleting
    those goes through the home screen, where it says what it is doing."""
    global STORE
    if STORE is None or STORE.closed:
        raise HTTPException(400, "No case is open")
    if not _is_temp_case(STORE.path):
        raise HTTPException(400, "Only a quick-look case can be discarded — "
                                 "delete a real case from the home screen")
    path = STORE.path
    old, STORE = STORE, None
    with contextlib.suppress(Exception):
        old.close()
    for suffix in ("", "-wal", "-shm", ".winnow-lock"):
        with contextlib.suppress(OSError):
            os.remove(path + suffix)
    with contextlib.suppress(Exception):
        instances.set_case(None)
    return {"ok": True}


@app.post("/api/case/compact")
def api_case_compact():
    """VACUUM the open case file. Long-running and explicitly analyst-
    triggered — see Store.compact for why it isn't automatic."""
    try:
        return store().compact()
    except ValueError as e:
        raise HTTPException(400, str(e))  # not enough free disk


# Entries per list (dirs and files each) in browse_dir's files mode. A
# named constant returned in the response ("limit"), so the frontend renders
# the number it was actually given instead of hardcoding a copy that lies
# the day this changes. Same pattern as store.MAX_SCAN_RESULTS.
BROWSE_LIST_CAP = 2000


def _is_loopback(request: Request) -> bool:
    """Whether the TCP peer is this machine. request.client comes from the
    socket's peer address (uvicorn), never from anything the client sends,
    so it can't be header-spoofed. "testclient" is Starlette's TestClient
    peer name — never a real IP, so allowing it can't admit a network
    peer."""
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "testclient")


class MakeDirBody(BaseModel):
    parent: str
    name: str


@app.post("/api/browse_dir/new")
def api_browse_dir_new(request: Request, body: MakeDirBody):
    """Create one directory inside `parent` — the folder picker's "New
    folder", so an analyst filing a case somewhere that doesn't exist yet
    doesn't have to leave and come back.

    LOOPBACK ONLY, unlike the folder listing beside it. Listing answers any
    peer the analyst chose to bind (the no-auth model is theirs to accept),
    but this WRITES, and "read a directory name" and "create a directory
    anywhere the server user can" are not the same risk.

    `name` is one path segment, checked rather than trusted: no separators,
    no `..`, nothing that resolves outside `parent`. The analyst can
    already type any path they like into the case-path box — this is a
    convenience over the same ground, not a new reach."""
    if not _is_loopback(request):
        raise HTTPException(403, "Creating folders is only allowed from this machine")
    name = (body.name or "").strip().strip(".")
    if not name:
        raise HTTPException(400, "Give the folder a name")
    if os.sep in name or (os.altsep and os.altsep in name) or name in (".", ".."):
        raise HTTPException(400, "A folder name can't contain a path separator")
    parent = os.path.abspath(os.path.expanduser(body.parent or ""))
    if not os.path.isdir(parent):
        raise HTTPException(400, f"No folder at {parent}")
    target = os.path.abspath(os.path.join(parent, name))
    # Belt and braces over the name check: whatever the string did, the
    # result has to sit directly inside the folder being browsed.
    if os.path.dirname(target) != parent:
        raise HTTPException(400, "That name would create a folder somewhere else")
    if os.path.exists(target):
        raise HTTPException(400, f"{name!r} already exists here")
    try:
        os.mkdir(target)
    except OSError as e:
        raise HTTPException(400, f"Could not create it: {e}")
    return {"path": target, "name": name}


@app.get("/api/browse_dir")
def api_browse_dir(request: Request, path: str = "", files: bool = False):
    """One directory level — backs the "Browse..." folder picker in the
    new-case modal and, with files=true, the import modal's "Add from this
    machine…" file picker (the fast path: a picked path imports in place
    with no upload copy). A regular browser file/folder input can't hand
    back a real filesystem path (sandboxed for security), and this is a
    local, single-analyst tool with no auth already (see CLAUDE.md) — the
    plain-text case-path field already lets the analyst create a case file
    anywhere on disk they can type a path to, so this is a more convenient
    way to reach the same place, not a new capability. GET, like every
    other read-only route, so no CSRF header is required, but that's fine:
    a cross-origin page can still fire the request, it just can't read the
    response (no CORS headers on it), same as every other GET here.

    files=true is loopback-only. Folder listing has always answered any
    peer the analyst chose to bind (--host 0.0.0.0 is on them, per the
    no-auth model), but a disk-wide filename+size enumeration is a bigger
    gift to a LAN scanner than a dir-name listing, and gating it costs a
    same-machine analyst nothing.

    Files come back with sizes and are NOT filtered — not by extension
    (what's importable is the frontend's call: its extension lists plus
    loaded plugin formats, which this route can't know) and not by dot
    prefix (on a mounted *nix image, .bash_history and .ssh/ are exactly
    the evidence the picker exists to reach; hiding them reads as "my
    file is missing"). The folder picker keeps its cosmetic dot filter —
    choosing where a case file goes is not evidence work. Both lists are
    capped: a listing is for picking, not enumerating a million-entry dir,
    and the cap bounds the work (heapq over the scandir iterator), not
    just the payload.

    A path that names a FILE (files mode) answers {"picked": {name, size},
    "path": <its dir>} instead of 400 — the typed-path box hands its text
    straight here, and os.path resolving it server-side is what makes a
    pasted Windows path or a file past the listing cap work at all."""
    if files and not _is_loopback(request):
        raise HTTPException(403, "The file picker is available from this machine only")
    base = os.path.abspath(os.path.expanduser(path)) if path else str(Path.home())
    if files and os.path.isfile(base):
        st = os.stat(base)
        return {"path": os.path.dirname(base), "parent": None,
                "picked": {"name": os.path.basename(base), "size": st.st_size}}
    if not os.path.isdir(base):
        raise HTTPException(400, f"Not a directory: {base}")
    dirs: list[str] = []
    file_entries: list[dict] = []
    truncated = False
    try:
        with os.scandir(base) as it:
            all_dirs, all_files = [], []
            for e in it:
                if e.is_dir():
                    # files mode lists dot-dirs too (see docstring); the
                    # folder picker keeps its cosmetic filter.
                    if files or not e.name.startswith("."):
                        all_dirs.append(e.name)
                elif files and e.is_file():
                    all_files.append(e)
        if files:
            truncated = len(all_dirs) > BROWSE_LIST_CAP or len(all_files) > BROWSE_LIST_CAP
            dirs = heapq.nsmallest(BROWSE_LIST_CAP, all_dirs, key=str.lower)
            for e in heapq.nsmallest(BROWSE_LIST_CAP, all_files, key=lambda e: e.name.lower()):
                try:
                    file_entries.append({"name": e.name, "size": e.stat().st_size})
                except OSError:
                    continue
        else:
            dirs = sorted(all_dirs, key=str.lower)
    except PermissionError:
        dirs = []
    parent = os.path.dirname(base)
    out = {"path": base, "parent": parent if parent != base else None, "dirs": dirs}
    if files:
        out["files"] = file_entries
        out["truncated"] = truncated
        out["limit"] = BROWSE_LIST_CAP
    return out


class PrefsBody(BaseModel):
    cases_dir: str | None = None


@app.get("/api/prefs")
def api_prefs():
    # A quick-look opened on a brand-new install would otherwise get the
    # first-run setup prompt stacked on top of the file the analyst just
    # double-clicked — the exact friction the association exists to remove.
    # Nothing is being filed anywhere yet; the cases-dir question can wait
    # until Save-as-a-case, or until they launch Winnow itself.
    quicklook = STORE is not None and _is_temp_case(STORE.path)
    return {"cases_dir": WS.machine_prefs.get("cases_dir"),
            # first run of this INSTANCE: nothing configured and no cases yet
            "first_run": (WS.machine_prefs.get("cases_dir") is None
                          and not WS.cases.list() and not quicklook)}


@app.post("/api/prefs")
def api_prefs_set(body: PrefsBody):
    if body.cases_dir is not None:
        path = os.path.abspath(os.path.expanduser(body.cases_dir.strip()))
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"Can't use that folder: {e}")
        WS.machine_prefs.set("cases_dir", path)
    return api_prefs()


@app.get("/api/cases")
def api_cases_list():
    out = []
    for c in WS.cases.list():
        rec = dict(c)
        if os.path.isfile(c["path"]):
            rec["exists"] = True
            try:
                ro = sqlite3.connect(f"file:{c['path']}?mode=ro", uri=True)
                row = ro.execute("SELECT count(*), COALESCE(sum(row_count),0) FROM sources").fetchone()
                ro.close()
                rec["source_count"], rec["row_count"] = row[0], row[1]
            except sqlite3.Error as e:
                rec["error"] = str(e)
        else:
            rec["exists"] = False
        out.append(rec)
    return out


@app.post("/api/cases")
def api_cases_create(body: CaseCreate):
    if not os.path.isfile(body.path):
        parent = os.path.dirname(os.path.abspath(body.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            _open_store(body.path).close()
        except Exception as e:
            raise HTTPException(400, f"Could not create case file: {e}")
    return WS.cases.create(body.path, body.name, body.group, body.notes)


@app.put("/api/cases/{case_id}")
def api_cases_update(case_id: int, body: CaseUpdate):
    try:
        return WS.cases.update(case_id, name=body.name, group=body.group, notes=body.notes)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/cases/{case_id}")
def api_cases_delete(case_id: int, delete_file: bool = False):
    if delete_file:
        rec = WS.cases.get(case_id)
        if rec and os.path.isfile(rec["path"]):
            if STORE is not None and os.path.abspath(STORE.path) == os.path.abspath(rec["path"]):
                raise HTTPException(400, "Close this case before deleting its file")
            os.remove(rec["path"])
    WS.cases.delete(case_id)
    return {"ok": True}


@app.get("/api/sources")
def api_sources():
    return store().list_sources()


@app.post("/api/ingest/path")
def api_ingest_path(body: IngestPath):
    if not os.path.isfile(body.path):
        raise HTTPException(400, f"No file at {body.path}")
    try:
        return store().ingest_csv(
            body.path, name=body.name, delimiter=body.delimiter, build_fts=body.build_fts,
            has_header=body.has_header, column_types=body.column_types,
        )
    except Exception as e:  # surface the real parser error to the UI
        raise HTTPException(400, str(e))


@app.post("/api/ingest/json/path")
def api_ingest_json_path(body: IngestJsonPath):
    """JSON sibling of api_ingest_path — same by-server-path ingest, for a
    file ingest_json can already read directly (it's always taken a plain
    filesystem path, even from the upload route's tempfile). Both exist for
    directory import (see api_ingest_dir_scan): the browser and server are
    the same machine there, so there's no reason to round-trip a file
    that's already on disk through a multipart upload."""
    if not os.path.isfile(body.path):
        raise HTTPException(400, f"No file at {body.path}")
    try:
        return store().ingest_json(
            body.path, name=body.name, flatten_mode=body.flatten_mode,
            flatten_depth=body.flatten_depth, build_fts=body.build_fts,
        )
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/ingest/dir/scan")
def api_ingest_dir_scan(body: DirectoryScan):
    """Preview for directory import — matches files under body.root against
    extension + include/exclude patterns without ingesting anything. See
    Store.scan_import_directory for the matching rules. Cheap enough (pure
    os.walk + fnmatch) to call on every pattern edit in the import modal,
    not just once behind an explicit button."""
    if not os.path.isdir(body.root):
        raise HTTPException(400, f"Not a directory: {body.root}")
    return store().scan_import_directory(
        body.root, recursive=body.recursive, extensions=body.extensions,
        include_patterns=body.include_patterns, exclude_patterns=body.exclude_patterns,
        filename_patterns=body.filename_patterns,
    )


@app.get("/api/import_profiles")
def api_import_profiles_list():
    return WS.import_profiles.list()


@app.post("/api/import_profiles")
def api_import_profiles_save(body: ImportProfileWrite):
    return WS.import_profiles.upsert(
        body.id, body.name, body.extensions, body.include_patterns,
        body.exclude_patterns, body.recursive,
    )


@app.delete("/api/import_profiles/{profile_id}")
def api_import_profiles_delete(profile_id: int):
    WS.import_profiles.delete(profile_id)
    return {"ok": True}


@app.post("/api/ingest/upload")
async def api_ingest_upload(
    file: UploadFile = File(...),
    build_fts: bool = Form(True),
    delimiter: str | None = Form(None),
    has_header: bool = Form(True),
    column_types: str | None = Form(None),  # JSON-encoded list[str], since multipart forms are flat
):
    suffix = Path(file.filename or "upload.csv").suffix or ".csv"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(4 << 20):
                out.write(chunk)
        types = json.loads(column_types) if column_types else None
        return store().ingest_csv(
            tmp, name=file.filename, build_fts=build_fts, delimiter=delimiter or None,
            has_header=has_header, column_types=types,
        )
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        # The rows are in the case file the moment ingest returns; the spool
        # was leaking its full size in the OS tempdir on every upload.
        with contextlib.suppress(OSError):
            os.remove(tmp)


def _decode_preview_bytes(raw: bytes) -> str:
    # Same UTF-16 BOM handling as the real ingest (store.sniff_text_encoding)
    # so the preview an analyst approves is the table they actually get —
    # PowerShell's UTF-16LE exports were previewing as NUL-riddled noise.
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


@app.post("/api/ingest/preview")
async def api_ingest_preview(
    file: UploadFile = File(...),
    delimiter: str | None = Form(None),
    has_header: bool = Form(True),
):
    raw = await file.read(512 * 1024)  # bounded sample — full parse happens at real ingest time
    text = _decode_preview_bytes(raw)
    try:
        return store().preview_csv_text(text, delimiter=delimiter or None, has_header=has_header)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/ingest/sqlite/preview")
async def api_ingest_sqlite_preview(file: UploadFile = File(...)):
    # Unlike the CSV preview (an in-memory byte sample — a delimited text
    # file can be sniffed from its first chunk), sqlite3 needs a real file
    # path, and a table's row count/likely-timestamp-columns aren't
    # knowable from a truncated prefix — so the *whole* file is written out
    # temporarily, then removed once preview_sqlite_tables is done with it.
    # Chromium DBs are small enough (single-digit to low-hundreds of MB)
    # that this round trip is cheap relative to the actual import.
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(4 << 20):
                out.write(chunk)
        return store().preview_sqlite_tables(tmp)
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        os.remove(tmp)


@app.post("/api/ingest/sqlite/upload")
async def api_ingest_sqlite_upload(
    file: UploadFile = File(...),
    table: str = Form(...),
    name: str | None = Form(None),
    build_fts: bool = Form(True),
    timestamp_columns: str | None = Form(None),  # JSON-encoded list[str]
):
    suffix = Path(file.filename or "upload.sqlite").suffix or ".sqlite"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(4 << 20):
                out.write(chunk)
        ts_cols = json.loads(timestamp_columns) if timestamp_columns else None
        return store().ingest_sqlite_table(
            tmp, table, name=name or f"{Path(file.filename or 'upload').stem}.{table}",
            build_fts=build_fts, timestamp_columns=ts_cols,
        )
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)


@app.post("/api/ingest/xlsx/preview")
async def api_ingest_xlsx_preview(file: UploadFile = File(...)):
    # Same shape as the SQLite preview, for the same reason: a zip's
    # central directory lives at the END of the file, so a workbook can't
    # be sniffed from a truncated prefix — spool the whole file, list its
    # sheets, remove the spool.
    suffix = Path(file.filename or "upload.xlsx").suffix or ".xlsx"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(4 << 20):
                out.write(chunk)
        return store().preview_xlsx_sheets(tmp)
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        os.remove(tmp)


@app.post("/api/ingest/json/preview")
async def api_ingest_json_preview(
    file: UploadFile = File(...),
    flatten_mode: str = Form("none"),
    flatten_depth: int = Form(0),
):
    # Like the SQLite preview (and unlike CSV's byte-sample one): a .json
    # document can't be safely truncated mid-structure, so the whole file
    # is written out temporarily and removed once the preview is done.
    suffix = Path(file.filename or "upload.json").suffix or ".json"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(4 << 20):
                out.write(chunk)
        return store().preview_json_file(tmp, flatten_mode=flatten_mode, flatten_depth=flatten_depth)
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        os.remove(tmp)


@app.post("/api/ingest/json/upload")
async def api_ingest_json_upload(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    flatten_mode: str = Form("none"),
    flatten_depth: int = Form(0),
    build_fts: bool = Form(True),
):
    suffix = Path(file.filename or "upload.json").suffix or ".json"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(4 << 20):
                out.write(chunk)
        return store().ingest_json(
            tmp, name=name or file.filename, flatten_mode=flatten_mode, flatten_depth=flatten_depth,
            build_fts=build_fts,
        )
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)


_JSON_INGEST_EXTS = {".json", ".jsonl", ".ndjson"}


def _ingest_kind_for_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in SQLITE_IMPORT_EXTENSIONS:
        return "sqlite"
    if suffix in XLSX_IMPORT_EXTENSIONS:
        return "xlsx"
    return "json" if suffix in _JSON_INGEST_EXTS else "csv"


class PreviewPath(BaseModel):
    """Path-based twin of the three upload previews, for files picked with
    the server-disk browser — the whole point of that flow is not copying
    the file, and the configure step shouldn't undo it. kind=None
    auto-detects by extension."""
    path: str
    kind: str | None = None
    # csv
    delimiter: str | None = None
    has_header: bool = True
    # json
    flatten_mode: str = "none"
    flatten_depth: int = 0


@app.post("/api/ingest/preview/path")
def api_ingest_preview_path(body: PreviewPath):
    if not os.path.isfile(body.path):
        raise HTTPException(400, f"No file at {body.path}")
    kind = body.kind or _ingest_kind_for_path(body.path)
    try:
        if kind == "csv":
            # Same bounded sample the upload preview reads — never the file.
            with open(body.path, "rb") as f:
                raw = f.read(512 * 1024)
            text = _decode_preview_bytes(raw)
            return store().preview_csv_text(text, delimiter=body.delimiter or None,
                                            has_header=body.has_header)
        if kind == "json":
            return store().preview_json_file(body.path, flatten_mode=body.flatten_mode,
                                             flatten_depth=body.flatten_depth)
        if kind == "xlsx":
            return store().preview_xlsx_sheets(body.path)
        return store().preview_sqlite_tables(body.path)
    except Exception as e:
        raise HTTPException(400, str(e))


class IngestJobPath(BaseModel):
    """Start a background ingest job for a file already on the server's
    disk — the directory-import loop and the import modal's server-disk
    picker, both of which exist because browser and server are the same
    machine here and round-tripping a file that's already on disk through
    a multipart upload copies it for nothing. kind=None auto-detects by
    extension; sqlite additionally needs `tables` (which tables to pull
    out is a real choice — see preview_sqlite_tables)."""
    path: str
    kind: str | None = None
    name: str | None = None
    build_fts: bool = True
    # csv options
    delimiter: str | None = None
    has_header: bool = True
    column_types: list[str] | None = None
    # json options
    flatten_mode: str = "none"
    flatten_depth: int = 0
    # sqlite options
    tables: list[dict] | None = None  # [{table, name?, timestamp_columns?}]


def _ingest_job_options(kind: str, *, build_fts: bool, delimiter=None, has_header=True,
                        column_types=None, flatten_mode="none", flatten_depth=0,
                        tables=None) -> dict:
    if kind == "csv":
        return {"build_fts": build_fts, "delimiter": delimiter,
                "has_header": has_header, "column_types": column_types}
    if kind == "json":
        return {"build_fts": build_fts, "flatten_mode": flatten_mode,
                "flatten_depth": flatten_depth}
    return {"build_fts": build_fts, "tables": tables or []}


@app.post("/api/ingest/jobs/path")
def api_ingest_job_path(body: IngestJobPath):
    if not os.path.isfile(body.path):
        raise HTTPException(400, f"No file at {body.path}")
    kind = body.kind or _ingest_kind_for_path(body.path)
    if kind == "sqlite" and not body.tables:
        raise HTTPException(400, "A sqlite import needs its tables picked first")
    if kind == "xlsx" and not body.tables:
        raise HTTPException(400, "An Excel import needs its sheets picked first")
    try:
        return store().start_ingest_job(
            kind, body.path, name=body.name,
            options=_ingest_job_options(
                kind, build_fts=body.build_fts, delimiter=body.delimiter,
                has_header=body.has_header, column_types=body.column_types,
                flatten_mode=body.flatten_mode, flatten_depth=body.flatten_depth,
                tables=body.tables,
            ),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/ingest/jobs/upload")
async def api_ingest_job_upload(
    file: UploadFile = File(...),
    kind: str = Form("csv"),
    name: str | None = Form(None),
    build_fts: bool = Form(True),
    delimiter: str | None = Form(None),
    has_header: bool = Form(True),
    column_types: str | None = Form(None),   # JSON-encoded list[str] — multipart forms are flat
    flatten_mode: str = Form("none"),
    flatten_depth: int = Form(0),
    tables: str | None = Form(None),         # sqlite: JSON [{table, name?, timestamp_columns?}]
):
    """Upload-then-job: the HTTP transfer *is* this request (the browser
    gets transfer progress from its own XHR events), and the moment the
    spool completes a background ingest job takes over — the response
    carries the job record, not the finished source. delete_after cleans
    the spooled tempfile whichever way the job ends."""
    suffix = Path(file.filename or "upload").suffix or ".dat"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(4 << 20):
                out.write(chunk)
        table_list = json.loads(tables) if tables else None
        types = json.loads(column_types) if column_types else None
        return store().start_ingest_job(
            kind, tmp, name=name or file.filename, delete_after=True,
            options=_ingest_job_options(
                kind, build_fts=build_fts, delimiter=delimiter or None,
                has_header=has_header, column_types=types,
                flatten_mode=flatten_mode, flatten_depth=flatten_depth,
                tables=table_list,
            ),
        )
    except Exception as e:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise HTTPException(400, str(e))


@app.get("/api/ingest/jobs")
def api_ingest_jobs():
    return {"jobs": store().list_ingest_jobs()}


@app.post("/api/ingest/jobs/{job_id}/cancel")
def api_ingest_job_cancel(job_id: int):
    return {"cancelled": store().cancel_ingest_job(job_id)}


@app.get("/api/plugins")
def api_plugins():
    """Everything the frontend needs to surface plugins: every installed
    plugin (enabled or not, loaded or failed-with-why), every registered
    ingest format (extensions/patterns/options — what routes a dropped or
    scanned file to a plugin parser), and where plugins load from so
    Settings → Plugins can say which folder installs land in.
    Case-independent; the toggle/install routes below return this same
    shape so the panel re-renders from whichever response it just got."""
    overrides = _case_plugin_overrides()
    plugins = PLUGINS.describe()
    for p in plugins:
        # `enabled` stays the effective state (it's what gates tabs/formats
        # and what the registry actually loaded); these two say WHY, so the
        # panel's scope dropdown can show provenance instead of guessing.
        p["machine_enabled"] = WS.plugin_prefs.machine_enabled(
            p["fs_name"], default_on=not p.get("bundled"))
        p["case_override"] = overrides.get(p["fs_name"])
    return {
        "api_version": plugin_api.PLUGIN_API_VERSION,
        "dirs": [str(d) for d in PLUGIN_DIRS],
        "case_open": STORE is not None and not STORE.closed,
        "plugins": plugins,
        "formats": PLUGINS.list_formats(),
        "tabs": PLUGINS.list_tabs(),
    }


@app.get("/plugin_assets/{fs_name}/{asset_path:path}")
def api_plugin_asset(fs_name: str, asset_path: str):
    """Serves a plugin folder's own files (its tab's ES module, CSS, any
    helper modules it import()s) — how a plugin ships UI without touching
    static/. Only enabled folder plugins are served: a disabled plugin's
    UI shouldn't keep loading any more than its Python should, and the
    resolved-path containment check means a crafted ../ path can't read
    outside the plugin's folder."""
    rec = next((p for p in PLUGINS.describe() if p["fs_name"] == fs_name and p["enabled"]), None)
    if rec is None:
        raise HTTPException(404, "No such plugin")
    root = Path(rec["path"]).resolve()
    if not root.is_dir():
        raise HTTPException(404, "Not a folder plugin")
    target = (root / asset_path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404, "No such asset")
    return FileResponse(target)


@app.api_route("/api/plugin/{fs_name}/{route:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def api_plugin_dispatch(fs_name: str, route: str, request: Request):
    """One dispatcher for every plugin-registered backend route (see
    plugin_api.PluginAPI.register_api) rather than mounting real FastAPI
    routes per plugin: routes come and go with Settings → Plugins toggles,
    and looking the handler up at request time in the live registry makes
    a reload instantly authoritative — no stale route objects to tear out
    of the app. The CSRF middleware already gates non-GET /api/* calls.
    Handlers get a plain PluginRequest and return JSON-able data;
    ValueError is the analyst-actionable 400 (same split api_view uses),
    anything else surfaces as the 500 it is."""
    entry = PLUGINS.get_api(fs_name, route)
    if entry is None:
        raise HTTPException(404, f"No plugin route {fs_name}/{route}")
    if request.method not in entry["methods"]:
        raise HTTPException(405, f"{request.method} not allowed on {fs_name}/{route}")
    body = None
    raw = await request.body()
    if raw:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(400, "Request body must be JSON")
    req = plugin_api.PluginRequest(
        request.method, route, dict(request.query_params), body, STORE,
    )
    try:
        return JSONResponse(entry["handler"](req))
    except ValueError as e:
        raise HTTPException(400, str(e))


class PluginToggle(BaseModel):
    fs_name: str   # the plugins/ entry's file/folder name — the identity that exists without importing
    enabled: bool | None = None          # legacy body: on/off everywhere
    scope: str | None = None             # on_all | off_all | on_case | off_case


@app.post("/api/plugins/toggle")
def api_plugins_toggle(body: PluginToggle):
    """Settings → Plugins checkbox. Persists to workspace/plugins.json and
    reloads the registry immediately — a disabled plugin's code is not
    merely unrouted, it is never imported on any later load (see
    PluginRegistry.load), which is the whole value of an off switch on
    something that runs with the app's privileges."""
    rec = next((p for p in PLUGINS.describe() if p["fs_name"] == body.fs_name), None)
    if rec is None:
        raise HTTPException(404, f"No installed plugin named {body.fs_name}")
    scope = body.scope
    if scope is None:
        if body.enabled is None:
            raise HTTPException(400, "Send scope (on_all/off_all/on_case/off_case) or the legacy enabled flag")
        scope = "on_all" if body.enabled else "off_all"
    if scope not in ("on_all", "off_all", "on_case", "off_case"):
        raise HTTPException(400, f"Unknown scope {scope!r}")
    default_on = not rec.get("bundled")
    if scope in ("on_all", "off_all"):
        # The "everywhere" scopes also clear this case's override — picking
        # them is a statement about every case, and a leftover override
        # silently exempting the open one would make the dropdown a liar.
        WS.plugin_prefs.set_machine_enabled(body.fs_name, scope == "on_all", default_on)
        if STORE is not None and not STORE.closed:
            overrides = _case_plugin_overrides()
            if body.fs_name in overrides:
                del overrides[body.fs_name]
                STORE.set_case_setting("plugin_overrides", json.dumps(overrides) if overrides else None)
    else:
        if STORE is None or STORE.closed:
            raise HTTPException(400, "Open a case first — per-case scopes live in the case file")
        overrides = _case_plugin_overrides()
        overrides[body.fs_name] = scope == "on_case"
        STORE.set_case_setting("plugin_overrides", json.dumps(overrides))
    _reload_plugins()
    return api_plugins()


class PluginBundleBody(BaseModel):
    name: str
    plugins: list[str] = []


@app.get("/api/plugin_bundles")
def api_plugin_bundles():
    return WS.plugin_bundles.list()


@app.post("/api/plugin_bundles")
def api_plugin_bundles_save(body: PluginBundleBody):
    try:
        return WS.plugin_bundles.save(body.name, body.plugins)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/plugin_bundles/{bundle_id}")
def api_plugin_bundles_delete(bundle_id: int):
    WS.plugin_bundles.delete(bundle_id)
    return {"ok": True}


@app.post("/api/plugin_bundles/{bundle_id}/apply")
def api_plugin_bundles_apply(bundle_id: int):
    """Set the open case's per-plugin overrides to exactly this bundle —
    every installed plugin gets an explicit on/off override, so the case's
    plugin set is the bundle regardless of machine defaults. One registry
    reload, not one per plugin."""
    if STORE is None or STORE.closed:
        raise HTTPException(400, "Open a case first — per-case scopes live in the case file")
    try:
        bundle = WS.plugin_bundles.get(bundle_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    wanted = set(bundle["plugins"])
    known = {p["fs_name"] for p in PLUGINS.describe()}
    overrides = _case_plugin_overrides()
    for fs_name in known:
        overrides[fs_name] = fs_name in wanted
    STORE.set_case_setting("plugin_overrides", json.dumps(overrides))
    _reload_plugins()
    return {"applied": bundle["name"],
            "enabled": sorted(wanted & known),
            "missing": sorted(wanted - known),  # in the bundle, not installed here
            "plugins": api_plugins()}


@app.post("/api/plugins/install")
async def api_plugins_install(
    files: list[UploadFile] = File(...),
    paths: str | None = Form(None),  # JSON list of relative paths aligned with files — folder installs; omitted for a single .py
    overwrite: bool = Form(False),
):
    """Install a plugin picked from the local disk (Settings → Plugins):
    the browser uploads a single .py file, or a whole folder via a
    webkitdirectory picker (every file inside, with its path relative to
    the picked folder), and this copies it into PLUGIN_DIRS[0] and reloads.
    The same consent model as dropping the file in plugins/ by hand — the
    analyst explicitly picked it — with the copying done for them.

    Every relative path is validated before anything is written: rejects
    absolute paths and any '..' component, so an upload can't write
    outside the plugins directory, and a folder install may only create
    one top-level entry (the plugin folder itself). __pycache__/*.pyc
    ride-alongs from a picked folder are dropped rather than copied.

    A plugin that installs but then fails to load (syntax error, bad
    register()) is still a *successful install* — the files are kept, the
    response carries the load error, and the panel shows it exactly as it
    would any other broken plugin; deleting or fixing it is the analyst's
    call, same as a hand-copied broken plugin."""
    try:
        rel_paths = json.loads(paths) if paths else [f.filename or "" for f in files]
    except json.JSONDecodeError:
        raise HTTPException(400, "paths must be a JSON list")
    if len(rel_paths) != len(files):
        raise HTTPException(400, "paths and files must align")

    keep: list[tuple[Path, UploadFile]] = []  # (relative path, upload)
    for rel, f in zip(rel_paths, files):
        p = Path(str(rel).replace("\\", "/"))
        if not rel or p.is_absolute() or ".." in p.parts or not p.parts:
            raise HTTPException(400, f"Unsafe path in upload: {rel!r}")
        if "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        keep.append((p, f))
    if not keep:
        raise HTTPException(400, "Nothing installable in the upload")

    if all(len(p.parts) == 1 for p, _ in keep):
        if len(keep) != 1 or keep[0][0].suffix != ".py":
            raise HTTPException(400, "A single-file plugin must be exactly one .py file")
        fs_name = keep[0][0].stem
        dest = PLUGIN_DIRS[0] / keep[0][0].name
    else:
        tops = {p.parts[0] for p, _ in keep}
        if len(tops) != 1:
            raise HTTPException(400, "A folder install must contain one top-level folder")
        fs_name = tops.pop()
        if not any(p.parts == (fs_name, "__init__.py") for p, _ in keep):
            raise HTTPException(400, f"Not a plugin package: no {fs_name}/__init__.py in the folder")
        dest = PLUGIN_DIRS[0] / fs_name

    if dest.exists() and not overwrite:
        # 409, not 400: the request is fine, the name is simply taken —
        # the frontend confirms and retries with overwrite=true.
        raise HTTPException(409, f"A plugin named {fs_name} is already installed")
    if overwrite and dest.exists():
        shutil.rmtree(dest) if dest.is_dir() else dest.unlink()

    PLUGIN_DIRS[0].mkdir(parents=True, exist_ok=True)
    for p, f in keep:
        out_path = PLUGIN_DIRS[0] / p
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as out:
            while chunk := await f.read(4 << 20):
                out.write(chunk)

    # Installing something states intent to use it — clear any stale
    # disabled mark left by an earlier install under the same name.
    WS.plugin_prefs.set_enabled(fs_name, True)
    _reload_plugins()
    rec = next((p for p in PLUGINS.describe() if p["fs_name"] == fs_name), None)
    return {"installed": fs_name, "error": rec["error"] if rec else None, **api_plugins()}


def _ingest_via_plugin(path: str, format_id: str, name: str | None,
                       options: dict, build_fts: bool) -> dict:
    """Shared by the path and upload plugin-ingest routes: resolve the
    format, run its parse, feed the result through Store.ingest_rows. The
    parse call itself runs outside Store.lock (it's pure file reading —
    only the row batches inside ingest_rows ever hold the lock), so a slow
    multi-GB parse doesn't freeze other requests any more than a big CSV
    import does."""
    fmt = PLUGINS.get_format(format_id)  # KeyError -> 400 at the route
    opts = fmt.resolve_options(options)
    result = fmt.parse(path, opts)
    if not isinstance(result, dict) or "columns" not in result or "rows" not in result:
        raise ValueError(f"Plugin format {format_id} returned no columns/rows")
    return store().ingest_rows(
        result["columns"], result["rows"],
        name=name or result.get("name") or os.path.basename(path),
        path=path, build_fts=build_fts,
        column_types=result.get("column_types"),
    )


@app.post("/api/ingest/plugin/path")
def api_ingest_plugin_path(body: IngestPluginPath):
    """Plugin sibling of api_ingest_path / api_ingest_json_path — same
    by-server-path ingest for directory import and scripted use, with the
    parsing done by a plugin-registered format instead of a built-in."""
    if not os.path.isfile(body.path):
        raise HTTPException(400, f"No file at {body.path}")
    try:
        return _ingest_via_plugin(body.path, body.format_id, body.name, body.options, body.build_fts)
    except Exception as e:  # surface the real parser error to the UI, same as the other ingest routes
        raise HTTPException(400, str(e))


@app.post("/api/ingest/plugin/upload")
async def api_ingest_plugin_upload(
    file: UploadFile = File(...),
    format_id: str = Form(...),
    name: str | None = Form(None),
    options: str | None = Form(None),  # JSON-encoded dict, since multipart forms are flat
    build_fts: bool = Form(True),
):
    suffix = Path(file.filename or "upload.bin").suffix or ".bin"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(4 << 20):
                out.write(chunk)
        opts = json.loads(options) if options else {}
        return _ingest_via_plugin(tmp, format_id, name or file.filename, opts, build_fts)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.delete("/api/source/{source_id}")
def api_drop_source(source_id: int):
    store().drop_source(source_id)
    return {"ok": True}


class TabOpenReq(BaseModel):
    open: bool


@app.post("/api/source/{source_id}/open")
def api_set_tab_open(source_id: int, body: TabOpenReq):
    """Toggles whether a source (or, via a negative id, a merge) has a
    visible tab — this is the 'close tab' action now; it never touches the
    underlying data. Hard delete is a separate, explicit action
    (DELETE /api/source/{id} / DELETE /api/merges/{id})."""
    store().set_tab_open(source_id, body.open)
    return {"ok": True}


class NicknameReq(BaseModel):
    nickname: str | None = None


@app.post("/api/source/{source_id}/nickname")
def api_set_source_nickname(source_id: int, body: NicknameReq):
    """Sets (or, with an empty value, clears) a source's display nickname;
    on a merge (negative id) it renames the merge. Returns the refreshed
    source record."""
    try:
        return store().set_source_nickname(source_id, body.nickname)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))


class MergeCreate(BaseModel):
    name: str
    source_ids: list[int]


@app.get("/api/merges")
def api_merges_list():
    return store().list_merges()


@app.post("/api/merges")
def api_merges_create(body: MergeCreate):
    try:
        return store().create_merge(body.name, body.source_ids)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))


@app.delete("/api/merges/{merge_id}")
def api_merges_delete(merge_id: int):
    store().delete_merge(merge_id)
    return {"ok": True}


@app.post("/api/view")
def api_view(spec: ViewSpec):
    try:
        return store().build_view(spec.source_id, spec.model_dump())
    except (ValueError, KeyError) as e:
        # Only the analyst-fixable failures are 400s: a bad filter fragment,
        # an unknown column, a source that's gone. Anything else is a defect
        # in here and should surface as a 500 — blanket-catching Exception
        # made a genuine SQL bug show up in the UI as "Filter error: ...",
        # blaming the analyst's filter for something they can't fix and
        # burying the traceback.
        raise HTTPException(400, str(e))


@app.post("/api/view/sql")
def api_view_sql(spec: ViewSpec):
    """The spec rendered as standalone SQL for the SQL pane — same
    400-on-analyst-fixable split as api_view."""
    try:
        return {"sql": store().spec_sql(spec.source_id, spec.model_dump())}
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))


class FindTimestampReq(BaseModel):
    view_id: str
    value: str
    column: str | None = None


@app.post("/api/view/find_ts")
def api_view_find_ts(body: FindTimestampReq):
    try:
        res = store().find_nearest_timestamp(body.view_id, body.value, body.column)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(409, str(e))  # view expired — same contract as /api/rows
    if res is None:
        raise HTTPException(404, "No row in this view has a usable timestamp")
    return res


class TagTimeBoundsReq(BaseModel):
    source_id: int
    tag_ids: list[int] = []
    column: str | None = None


@app.post("/api/tag_time_bounds")
def api_tag_time_bounds(body: TagTimeBoundsReq):
    try:
        return store().tag_time_bounds(body.source_id, body.tag_ids, body.column)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/view/{view_id}")
def api_view_close(view_id: str):
    store().close_view(view_id)
    return {"ok": True}


class CancelOp(BaseModel):
    token: str


@app.post("/api/cancel_op")
def api_cancel_op(body: CancelOp):
    """Interrupts the in-flight cancellable operation (view/timeline build,
    group summary) started with this client-generated token. A miss — the
    operation already finished, or never started — is a no-op, reported as
    cancelled: false."""
    return {"cancelled": store().cancel_op(body.token)}


@app.get("/api/group_summary")
def api_group_summary(view_id: str, column: str, order: str = "count", direction: str | None = None,
                       limit: int = 1000, path: str = "", op_token: str | None = None,
                       bucket_datetime: bool = True):
    try:
        # `path` (the outer levels already fixed by nested grouping) is a
        # JSON-encoded list — GET query params don't carry structured data,
        # and this stays a GET (not POST) so it keeps the same cacheable,
        # side-effect-free shape as every other view-summary read.
        path_list = json.loads(path) if path else None
        return JSONResponse(store().group_summary(view_id, column, order=order, direction=direction,
                                                  limit=min(limit, 5000), path=path_list,
                                                  op_token=op_token, bucket_datetime=bucket_datetime))
    except KeyError as e:
        raise HTTPException(409, str(e))


class GroupExpand(BaseModel):
    view_id: str
    column: str
    value: Any = None
    path: list[dict] = []


@app.post("/api/group_expand")
def api_group_expand(body: GroupExpand):
    try:
        return store().expand_group(body.view_id, body.column, body.value, path=body.path or None)
    except KeyError as e:
        raise HTTPException(409, str(e))


class WhereValidate(BaseModel):
    source_id: int
    fragment: str


@app.post("/api/filter/validate")
def api_filter_validate(body: WhereValidate):
    try:
        store().validate_where_fragment(body.source_id, body.fragment)
        return {"ok": True}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/rows")
def api_rows(view_id: str, start: int = 0, count: int = 200):
    try:
        # Defense-in-depth ceiling against a malformed/huge count, not tied
        # to app.js's own PAGE (5000) except that it must stay comfortably
        # above it — a cap below PAGE would silently truncate every page
        # fetch, since ensurePage() trusts the response to have PAGE rows.
        #
        # Wrapped in JSONResponse here (and on the other hot, large-payload
        # reads below): returning a Response makes FastAPI skip
        # jsonable_encoder's per-value walk over every cell of every row —
        # pure overhead for payloads that are already plain
        # str/int/None — and go straight to json.dumps. This endpoint is
        # hit on every scroll, so the saving is felt continuously.
        return JSONResponse(store().fetch_rows(view_id, start, min(count, 10000)))
    except KeyError as e:
        raise HTTPException(409, str(e))


@app.get("/api/tag_positions")
def api_tag_positions(view_id: str):
    try:
        return JSONResponse(store().tag_positions(view_id))
    except KeyError as e:
        raise HTTPException(409, str(e))


@app.get("/api/row_position")
def api_row_position(view_id: str, source_id: int, rid: int):
    try:
        return {"pos": store().find_position(view_id, source_id, rid)}
    except KeyError as e:
        raise HTTPException(409, str(e))


@app.get("/api/column_values")
def api_column_values(source_id: int, column: str, limit: int = 200):
    try:
        return JSONResponse(store().column_values(source_id, column, min(limit, 2000)))
    except KeyError:
        raise HTTPException(404, "No such column")


@app.get("/api/column_indexes")
def api_column_indexes(source_id: int):
    """The auto-created filter indexes on a source (see
    Store._ensure_column_index_building) — nothing else surfaces them, and
    they cost real disk."""
    try:
        return store().list_column_indexes(source_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/column_indexes")
def api_column_index_drop(source_id: int, column: str):
    try:
        store().drop_column_index(source_id, column)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.get("/api/column_maxlen")
def api_column_maxlen(source_id: int):
    try:
        return store().column_max_lengths(source_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/tags")
def api_tags(source_id: int | None = None):
    out = {"tags": store().list_tags()}
    if source_id is not None:
        out.update(store().tag_counts(source_id))
    return out


@app.get("/api/tag_counts")
def api_tag_counts(view_id: str):
    """Tag counts scoped to one view — what the ribbon shows once a filter
    or search is on. 409 on an expired view, same contract as every other
    view-keyed read."""
    try:
        return store().tag_counts_in_view(view_id)
    except KeyError as e:
        raise HTTPException(409, str(e))


@app.post("/api/tags")
def api_tag_upsert(body: TagDef):
    return store().upsert_tag(body.id, body.name, body.color, body.hotkey)


@app.delete("/api/tags/{tag_id}")
def api_tag_delete(tag_id: int):
    store().delete_tag(tag_id)
    return {"ok": True}


class DefaultTagsWrite(BaseModel):
    tags: list[dict]


@app.get("/api/settings/default_tags")
def api_default_tags_get():
    return WS.tags.get()


@app.post("/api/settings/default_tags")
def api_default_tags_save(body: DefaultTagsWrite):
    return WS.tags.save(body.tags)


class AppSettingsWrite(BaseModel):
    default_ts_format: str | None = None


@app.get("/api/settings/app")
def api_app_settings_get():
    return WS.app_settings.get()


@app.post("/api/settings/app")
def api_app_settings_save(body: AppSettingsWrite):
    try:
        return WS.app_settings.save(body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))


class CaseSettingWrite(BaseModel):
    ts_format: str | None = None


@app.get("/api/case_settings")
def api_case_settings_get():
    return store().get_case_settings()


@app.post("/api/case_settings")
def api_case_settings_save(body: CaseSettingWrite):
    """A blank/absent ts_format clears the case override, so the case falls
    back to the system-wide default rather than pinning today's value."""
    if body.ts_format is not None and body.ts_format not in WS.AppSettings.TS_FORMATS | {""}:
        raise HTTPException(400, f"Unknown timestamp format: {body.ts_format}")
    store().set_case_setting("ts_format", body.ts_format)
    return store().get_case_settings()


# ------------------------------------------------------------ derived columns


class DerivedCreate(BaseModel):
    source_id: int
    name: str
    input_column: str
    op_id: str
    params: dict = {}


class DerivedProbe(BaseModel):
    source_id: int
    column: str
    op_id: str | None = None
    params: dict = {}


class RederiveWrite(BaseModel):
    params: dict = {}


class DerivedBatchItem(BaseModel):
    name: str
    input_column: str
    op_id: str
    params: dict = {}


class DerivedBatchCreate(BaseModel):
    source_id: int
    columns: list[DerivedBatchItem]


class StructProbe(BaseModel):
    source_id: int
    column: str


@app.get("/api/derived/ops")
def api_derived_ops():
    return store().list_derived_ops()


@app.get("/api/derived")
def api_derived_list(source_id: int):
    return store().list_derived_columns(source_id)


@app.post("/api/derived")
def api_derived_create(body: DerivedCreate):
    try:
        return store().add_derived_column(
            body.source_id, body.name, body.input_column, body.op_id, body.params
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/derived/{def_id}")
def api_derived_delete(def_id: int):
    try:
        store().remove_derived_column(def_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.post("/api/derived/{def_id}/rederive")
def api_derived_rederive(def_id: int, body: RederiveWrite):
    try:
        return store().rederive_column(def_id, body.params)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/derived/batch")
def api_derived_create_batch(body: DerivedBatchCreate):
    """Several derived columns at once, backfilled in one pass — what
    flattening a JSON/XML column into its fields turns into."""
    try:
        return store().add_derived_columns(
            body.source_id, [c.model_dump() for c in body.columns]
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/derived/paths")
def api_derived_paths(body: StructProbe):
    """The fields inside a JSON/XML column, with per-field coverage across
    a sample — the picker behind "flatten this into columns"."""
    try:
        return store().detect_struct_paths(body.source_id, body.column)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/derived/detect")
def api_derived_detect(body: DerivedProbe):
    try:
        return store().detect_timestamp_format(body.source_id, body.column)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/derived/suggestions")
def api_derived_suggestions(source_id: int):
    try:
        return store().detect_source_suggestions(source_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/derived/preview")
def api_derived_preview(body: DerivedProbe):
    if not body.op_id:
        raise HTTPException(400, "op_id is required")
    try:
        return store().preview_derived(body.source_id, body.column, body.op_id, body.params)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/derived/{def_id}/unparsed_filter")
def api_derived_unparsed_filter(def_id: int):
    """The advanced-filter fragment for "show me the rows that didn't
    parse" — built server-side so the UI never has to quote a column name
    into SQL itself."""
    try:
        return {"sql": store().unparsed_where_fragment(def_id)}
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/row_tags/undo")
def api_row_tags_undo_peek():
    """What Ctrl+Z would reverse, for the menu label and its enabled state."""
    return store().undo_peek()


@app.post("/api/row_tags/undo")
def api_row_tags_undo():
    try:
        return store().undo_last_tag_change()
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/row_tags")
def api_row_tags(body: TagWrite):
    if body.pairs:
        return store().set_tags_pairs(body.pairs, body.tag_id, body.on)
    return store().set_tags(body.source_id, body.rids, body.tag_id, body.on)


class TagViewWrite(BaseModel):
    view_id: str
    tag_id: int
    on: bool = True
    exclude: list[list[int]] = []  # [[source_id, rid], ...] — rows to leave alone (select-all minus a few)


@app.post("/api/row_tags/view")
def api_row_tags_view(body: TagViewWrite):
    try:
        return store().tag_view(body.view_id, body.tag_id, body.on, body.exclude)
    except KeyError as e:
        raise HTTPException(409, str(e))


@app.post("/api/note")
def api_note(body: NoteWrite):
    store().set_note(body.source_id, body.rid, body.note)
    return {"ok": True}


@app.post("/api/layout")
def api_layout_save(body: LayoutWrite):
    store().save_layout(body.source_id, body.payload)
    return {"ok": True}


@app.get("/api/layout")
def api_layout_get(source_id: int):
    return store().get_layout(source_id) or {}


@app.get("/api/saved_views")
def api_saved_views(source_id: int):
    return store().list_saved_views(source_id)


@app.post("/api/saved_views")
def api_saved_view_add(body: SavedViewWrite):
    return store().save_view(body.source_id, body.name, body.payload)


@app.delete("/api/saved_views/{view_id}")
def api_saved_view_del(view_id: int):
    store().delete_saved_view(view_id)
    return {"ok": True}


class NicknameWrite(BaseModel):
    col_names: list[str]
    nickname: str


@app.get("/api/header_nicknames")
def api_header_nicknames_list():
    return WS.header_nicknames.list()


@app.post("/api/header_nicknames")
def api_header_nicknames_save(body: NicknameWrite):
    return WS.header_nicknames.save(body.col_names, body.nickname)


@app.delete("/api/header_nicknames/{nickname_id}")
def api_header_nicknames_delete(nickname_id: int):
    WS.header_nicknames.delete(nickname_id)
    return {"ok": True}


class TimelineTemplateWrite(BaseModel):
    col_names: list[str]
    type_label: str
    timestamp_column: str | None = None
    body_columns: list[str] = []


@app.get("/api/timeline_templates")
def api_timeline_templates_list():
    return WS.timeline_templates.list()


@app.post("/api/timeline_templates")
def api_timeline_templates_save(body: TimelineTemplateWrite):
    return WS.timeline_templates.save(body.col_names, body.type_label, body.timestamp_column, body.body_columns)


@app.delete("/api/timeline_templates/{template_id}")
def api_timeline_templates_delete(template_id: int):
    WS.timeline_templates.delete(template_id)
    return {"ok": True}


def _resolve_timeline_configs() -> dict[int, dict]:
    """Matches every real source currently in the case against workspace.
    timeline_templates by header set (order/case-independent, same
    convention every header-set-keyed workspace store uses), returning
    only the sources that actually matched — build_timeline already
    supplies its own per-source defaults for anything missing here."""
    templates = WS.timeline_templates.list()
    by_key = {tuple(t["col_names"]): t for t in templates}
    configs: dict[int, dict] = {}
    for src in store().list_sources():
        key = tuple(sorted(c["name"].strip().lower() for c in src["columns"]))
        tmpl = by_key.get(key)
        if tmpl:
            configs[src["id"]] = {
                "timestamp_column": tmpl.get("timestamp_column"),
                "body_columns": tmpl.get("body_columns"),
                "type_label": tmpl.get("type_label"),
            }
    return configs


class TimelineBuild(BaseModel):
    tag_ids: list[int] = []
    op_token: str | None = None


@app.post("/api/timeline")
def api_timeline_build(body: TimelineBuild):
    return store().build_timeline(_resolve_timeline_configs(), body.tag_ids or None,
                                  op_token=body.op_token)


@app.get("/api/timeline_rows")
def api_timeline_rows(view_id: str, start: int = 0, count: int = 200):
    try:
        return JSONResponse(store().fetch_timeline_rows(view_id, start, count))
    except KeyError as e:
        raise HTTPException(409, str(e))


class SavedFilterCreate(BaseModel):
    name: str
    col_names: list[str]
    payload: dict


class SavedFilterUpdate(BaseModel):
    """Every field optional — a rename sends just `name`, the Filter
    builder's "Update" button sends just `payload`. See
    workspace.SavedFilters.update for why col_names is rarely sent."""
    name: str | None = None
    col_names: list[str] | None = None
    payload: dict | None = None


@app.get("/api/saved_filters")
def api_saved_filters_list():
    return WS.filters.list()


@app.post("/api/saved_filters")
def api_saved_filters_create(body: SavedFilterCreate):
    return WS.filters.create(body.name, body.col_names, body.payload)


@app.put("/api/saved_filters/{filter_id}")
def api_saved_filters_update(filter_id: int, body: SavedFilterUpdate):
    try:
        return WS.filters.update(
            filter_id, name=body.name, col_names=body.col_names, payload=body.payload,
        )
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/saved_filters/{filter_id}")
def api_saved_filters_delete(filter_id: int):
    WS.filters.delete(filter_id)
    return {"ok": True}


class SavedFilterReorder(BaseModel):
    ids: list[int]  # every id sharing one header set, in the new desired order


@app.post("/api/saved_filters/reorder")
def api_saved_filters_reorder(body: SavedFilterReorder):
    return WS.filters.reorder(body.ids)


@app.get("/api/saved_filters/export")
def api_saved_filters_export():
    data = WS.filters.export_all()
    return JSONResponse(
        data,
        headers={"Content-Disposition": 'attachment; filename="winnow-filters.json"'},
    )


@app.post("/api/saved_filters/import")
async def api_saved_filters_import(file: UploadFile = File(...), merge: bool = Form(True)):
    try:
        data = json.loads(await file.read())
    except Exception as e:
        raise HTTPException(400, f"Not a valid filters file: {e}")
    added = WS.filters.import_all(data, merge=merge)
    return {"added": added}


class ColumnLayoutSave(BaseModel):
    col_names: list[str]
    order: list[str]
    columns: dict


@app.get("/api/column_layouts/find")
def api_column_layout_find(col_names: list[str] = Query(default=[])):
    """Looks up the saved default layout for a header set — the frontend
    calls this on opening a brand-new source (one with no per-source
    /api/layout of its own yet) to seed order/visibility from whatever the
    analyst last saved for that same set of column names."""
    rec = WS.column_layouts.find(col_names)
    return rec or {}


@app.post("/api/column_layouts")
def api_column_layout_save(body: ColumnLayoutSave):
    return WS.column_layouts.save(body.col_names, body.order, body.columns)


@app.get("/api/session/{source_id}")
def api_session_export(source_id: int):
    data = store().export_session(source_id)
    name = os.path.splitext(data["source"]["name"])[0]
    return JSONResponse(
        data,
        headers={"Content-Disposition": f'attachment; filename="{name}.tls.json"'},
    )


@app.post("/api/session/{source_id}")
async def api_session_import(source_id: int, file: UploadFile = File(...), merge: bool = Form(True)):
    try:
        session = json.loads(await file.read())
    except Exception as e:
        raise HTTPException(400, f"Not a valid session file: {e}")
    return store().import_session(source_id, session, merge=merge)


@app.get("/api/case_session")
def api_case_session_export():
    data = store().export_case_session()
    return JSONResponse(
        data,
        headers={"Content-Disposition": 'attachment; filename="case-session.winnow_case.json"'},
    )


@app.post("/api/case_session")
async def api_case_session_import(file: UploadFile = File(...), merge: bool = Form(True)):
    try:
        session = json.loads(await file.read())
    except Exception as e:
        raise HTTPException(400, f"Not a valid session file: {e}")
    return store().import_case_session(session, merge=merge)


class SessionSaveReq(BaseModel):
    name: str


class CaseSessionSave(BaseModel):
    name: str


class CaseSessionRename(BaseModel):
    name: str


class CaseSessionAdopt(BaseModel):
    """A session file received from another analyst. Recording it and
    applying it are separate calls on purpose — taking someone's work into
    your case is a decision, not a side effect of opening their file."""
    name: str
    session: dict


@app.get("/api/case_sessions")
def api_case_sessions_list():
    """Sessions stored IN the case file. These travel with the .db; the
    older /api/sessions writes files beside it."""
    return {"sessions": store().list_sessions()}


@app.post("/api/case_sessions")
def api_case_sessions_save(body: CaseSessionSave):
    try:
        return store().save_session(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/case_sessions/{name}/load")
def api_case_sessions_load(name: str, merge: bool = True):
    try:
        return store().load_session(name, merge=merge)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/case_sessions/adopt")
def api_case_sessions_adopt(body: CaseSessionAdopt):
    try:
        return store().adopt_session(body.name, body.session)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/case_sessions/{name}/rename")
def api_case_sessions_rename(name: str, body: CaseSessionRename):
    try:
        return store().rename_session(name, body.name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/case_sessions/{name}")
def api_case_sessions_delete(name: str):
    store().delete_session(name)
    return {"ok": True}


@app.get("/api/case_sessions/{name}/download")
def api_case_sessions_download(name: str):
    """The hand-off: a stored session as a file, for sending to another
    analyst. The only reason a session file exists now."""
    try:
        data = store().get_session(name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "session"
    return JSONResponse(data, headers={
        "Content-Disposition": f'attachment; filename="{safe}.winnow_case.json"'})


class NewSessionReq(BaseModel):
    """save_as is optional but strongly encouraged by the UI — clearing
    without saving is the one irreversible move here."""
    save_as: str | None = None


@app.post("/api/case_sessions/new")
def api_case_sessions_new(body: NewSessionReq):
    """Start a fresh pass: save the current work (if named) and clear the
    live tags and notes. Layouts, derived columns and SQL tabs stay."""
    try:
        return store().start_new_session(body.save_as)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/case_sessions/diff")
def api_case_sessions_diff(left: str, right: str):
    """What changed between two sessions — the QC review. Either side may
    be the live case (`__live__`)."""
    try:
        return JSONResponse(store().diff_sessions(left, right))
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/sessions")
def api_sessions_list():
    return store().list_named_sessions()


@app.post("/api/sessions")
def api_sessions_save(body: SessionSaveReq):
    return store().save_named_session(body.name)


@app.post("/api/sessions/{name}/load")
def api_sessions_load(name: str, merge: bool = True):
    try:
        return store().load_named_session(name, merge=merge)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/sessions/{name}")
def api_sessions_delete(name: str):
    store().delete_named_session(name)
    return {"ok": True}


class SqlToTable(BaseModel):
    sql: str
    name: str
    force: bool = False


@app.post("/api/sql/to_table")
def api_sql_to_table(body: SqlToTable):
    """Land a pane query's result as a new source. Soft 500k cap: over it
    the response asks for confirmation ({needs_confirm, rows}); resend
    with force=true to proceed."""
    try:
        res = store().sql_to_table(body.sql, body.name, force=body.force)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except sqlite3.Error as e:
        raise HTTPException(400, str(e))
    return res


@app.get("/api/export")
def api_export(view_id: str, tagged_only: bool = False, filename: str = "timeline-export.csv"):
    try:
        gen = store().export_view_csv(view_id, tagged_only)
    except KeyError as e:
        raise HTTPException(409, str(e))
    return StreamingResponse(
        gen,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/export/tagged_xlsx")
def api_export_tagged_xlsx(filename: str = "tagged-export.xlsx"):
    buf = store().export_tagged_xlsx()
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class SearchAllReq(BaseModel):
    query: str = ""
    terms: list[dict] = []  # advanced mode: [{term, connector: "AND"|"OR", exclude: bool}]


@app.post("/api/search_all")
def api_search_all(body: SearchAllReq):
    """Synchronous whole-sweep search. Kept for scripted/one-shot use; the
    UI goes through the job routes below so it can show partial results and
    let the analyst close the modal mid-sweep."""
    return store().search_all_sources(body.query, body.terms)


@app.post("/api/search_all/start")
def api_search_all_start(body: SearchAllReq):
    return store().start_search_all_job(body.query, body.terms)


@app.get("/api/search_all/job")
def api_search_all_job(job_id: int | None = None):
    """404 (not an empty result) when the requested job is gone — either
    superseded by a newer one or from a case that's since been closed. The
    poller treats that as "stop polling" rather than "no matches"."""
    job = store().get_search_all_job(job_id)
    if job is None:
        raise HTTPException(404, "No such search job")
    return job


@app.post("/api/search_all/cancel")
def api_search_all_cancel(job_id: int | None = None):
    return {"cancelled": store().cancel_search_all_job(job_id)}


@app.post("/api/sql")
def api_sql(body: SqlQuery):
    try:
        return store().run_sql(body.sql, body.limit)
    except Exception as e:
        raise HTTPException(400, str(e))


class SqlTabCreate(BaseModel):
    name: str
    sql: str = ""


class SqlTabUpdate(BaseModel):
    name: str | None = None
    sql: str | None = None


class SqlTabReorder(BaseModel):
    ids: list[int]


@app.get("/api/sql_tabs")
def api_sql_tabs_list():
    return store().list_sql_tabs()


@app.post("/api/sql_tabs")
def api_sql_tabs_create(body: SqlTabCreate):
    return store().create_sql_tab(body.name, body.sql)


@app.put("/api/sql_tabs/{tab_id}")
def api_sql_tabs_update(tab_id: int, body: SqlTabUpdate):
    try:
        return store().update_sql_tab(tab_id, name=body.name, sql=body.sql)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/sql_tabs/{tab_id}")
def api_sql_tabs_delete(tab_id: int):
    store().delete_sql_tab(tab_id)
    return {"ok": True}


@app.post("/api/sql_tabs/reorder")
def api_sql_tabs_reorder(body: SqlTabReorder):
    return store().reorder_sql_tabs(body.ids)


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def _assoc_post(port: int, route: str, payload: dict, timeout: float = 30.0) -> dict | None:
    """POST to a sibling instance; None means it declined or died — the
    launcher just falls through to the next option."""
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — any failure means "this instance won't take it"
        return None


def _recent_temp_instance(alive: list[dict], max_age_s: float = 120.0) -> dict | None:
    """A just-started quick-look instance — where the rest of a multi-file
    double-click belongs. Windows delivers one launcher process per
    selected file, so five files selected together arrive as five
    invocations within a second or two; without this they would open five
    windows on five cases."""
    now = time.time()
    for i in alive:
        case = i.get("case_path") or ""
        if not case or not _is_temp_case(case):
            continue
        try:
            started = time.mktime(time.strptime(i["started_at"], "%Y-%m-%dT%H:%M:%S"))
        except (KeyError, ValueError):
            continue
        if now - started <= max_age_s:
            return i
    return None


def _run_assoc(files: list[str], no_browser: bool = False) -> int | None:
    """The file-association launch. Returns an exit code when the work was
    handed to an existing instance, or None when this process should carry
    on and BECOME the server (main() falls through with the right args).

    Order of preference: a case file opens as a case wherever possible; a
    fresh quick-look joins a seconds-old one (multi-select), else lands on
    an idle instance, else this process spawns. A busy instance is never
    touched — its windows belong to whatever the analyst is doing there."""
    files = [os.path.abspath(f) for f in files]
    for f in files:
        if not os.path.isfile(f):
            print(f"No file at {f}", file=sys.stderr)
            return 2
    alive = instances.running()

    if len(files) == 1 and is_winnow_case_file(files[0]):
        case = files[0]
        for i in alive:   # already open somewhere — just show that window
            if i.get("case_path") and os.path.abspath(i["case_path"]) == case:
                if not no_browser:
                    browser.open_when_ready(f"http://127.0.0.1:{i['port']}", "127.0.0.1", i["port"])
                return 0
        idle = instances.find_idle()
        if idle and _assoc_post(idle["port"], "/api/case/open", {"path": case}) is not None:
            if not no_browser:
                browser.open_when_ready(f"http://127.0.0.1:{idle['port']}", "127.0.0.1", idle["port"])
            return 0
        return None   # become the server, opening this case

    recent = _recent_temp_instance(alive)
    if recent and _assoc_post(recent["port"], "/api/assoc/open", {"files": files}) is not None:
        return 0   # its window is already up; the tables appear in it
    idle = instances.find_idle()
    if idle and _assoc_post(idle["port"], "/api/assoc/open", {"files": files}) is not None:
        if not no_browser:
            browser.open_when_ready(f"http://127.0.0.1:{idle['port']}", "127.0.0.1", idle["port"])
        return 0
    return None   # become the server


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    global STORE
    ap = argparse.ArgumentParser(description="Winnow")
    ap.add_argument("--case", default=None, help="SQLite case file (created if missing). Omit to land on the home screen.")
    ap.add_argument("--open", dest="open_files", nargs="*", default=[], help="CSVs to ingest at startup")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--force", action="store_true",
                    help="Open --case even if another Winnow already has it open")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--no-fts", action="store_true", help="Skip full-text index (faster import)")
    ap.add_argument("--assoc", nargs="+", metavar="FILE",
                    help="opened via a file association: reuse a running Winnow when "
                         "that is safe, otherwise become one on a free port, and land "
                         "data files in a temporary quick-look case")
    ap.add_argument("--no-idle-shutdown", action="store_true",
                    help="keep running even with no browser connected — for driving "
                         "the API from scripts")
    ap.add_argument("--no-browser", action="store_true",
                    help="don't open a window at all")
    ap.add_argument("--browser-tab", action="store_true",
                    help="open an ordinary browser tab instead of an app window "
                         "(the pre-app-mode behaviour)")
    ap.add_argument("--browser-profile", metavar="DIR", default=None,
                    help="give the app window its own browser profile directory. "
                         "Isolates Winnow from your extensions and gives it a "
                         "separate taskbar entry — but appearance, keybindings and "
                         "panel sizes live in that profile's localStorage, so the "
                         "first run with a new one starts from defaults")
    ap.add_argument("--plugins-dir", action="append", default=[], metavar="DIR",
                    help="Extra plugin directory (plugins/ next to server.py and $WINNOW_PLUGINS_DIR are always scanned; repeatable)")
    args = ap.parse_args()

    if args.plugins_dir:
        global PLUGIN_DIRS
        PLUGIN_DIRS = _plugin_dirs(args.plugins_dir)
        _reload_plugins()
    for p in PLUGINS.describe():
        if p["error"]:
            print(f"Plugin FAILED: {p['name']} ({p['path']}): {p['error']}", file=sys.stderr)
        elif not p["enabled"]:
            print(f"Plugin disabled: {p['name']} (toggle in Settings → Plugins)")
        else:
            # Formats *and* tabs — a tab-only plugin reporting "no formats"
            # reads like a failed load when it's a perfectly good plugin.
            what = ", ".join(p["formats"] + p["tabs"]) or "registered nothing"
            print(f"Plugin loaded: {p['name']}" + (f" v{p['version']}" if p["version"] else "") + f" ({what})")

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding to {args.host} instead of loopback. Winnow has no "
            "login and no access control — anyone who can reach this address on the "
            "network can open, import into, or query any case this server has access to. "
            "Only do this on a trusted, isolated network.",
            file=sys.stderr,
        )

    if args.assoc:
        handed_off = _run_assoc(args.assoc, no_browser=args.no_browser)
        if handed_off is not None:
            time.sleep(1.5)   # let the sibling's window launch before we vanish
            sys.exit(handed_off)
        # Nobody could take it — this process becomes the server, on its own
        # port so it can never collide with a Winnow already listening.
        args.port = _free_port()
        if len(args.assoc) == 1 and is_winnow_case_file(args.assoc[0]):
            args.case = args.assoc[0]
        else:
            args.case = _new_temp_case_path()
            # NOT via --open: that startup loop is ingest_csv only, and an
            # associated file may be a workbook or a SQLite db. These go
            # through the kind-aware quick-look ingest after the store opens.
            args.assoc_ingest = list(args.assoc)

    case_path = args.case or (f"case{CASE_SUFFIX}" if args.open_files else None)
    url = f"http://{args.host}:{args.port}"
    if case_path:
        # Refuse rather than warn: this entrypoint is scriptable, a warning
        # scrolls past, and opening a case twice is the failure the lock
        # exists to catch. --force is the deliberate way through, and a
        # holder that has actually died reads as free (stale heartbeat).
        holder = probe_case_lock(case_path) if not args.force else None
        if holder:
            print(
                f"ERROR: {os.path.abspath(case_path)} is already open in another Winnow.\n"
                f"  {describe_case_lock(holder)}\n"
                "  Opening one case in two servers has no cache invalidation between them, "
                "and on a network share WAL locking doesn't work at all.\n"
                "  Pass --force to open it anyway.",
                file=sys.stderr,
            )
            sys.exit(1)
        STORE = _open_store(case_path)
        legacy_presets = STORE.pop_legacy_presets()
        if legacy_presets:
            WS.filters.import_all({"filters": legacy_presets}, merge=True)
        if not _is_temp_case(case_path):
            # Quick-look cases stay OFF the home screen until the analyst
            # saves them — an unregistered temp case is the whole point.
            WS.cases.create(case_path, name=os.path.splitext(os.path.basename(case_path))[0])
            WS.cases.touch_opened(WS.cases.find_by_path(case_path)["id"])
        for f in getattr(args, "assoc_ingest", []):
            try:
                job = _assoc_ingest(f)
                print(f"Quick-look import queued: {job['file']} ({job['kind']})", flush=True)
            except Exception as e:  # noqa: BLE001 — same reasoning as the
                # /api/assoc/open loop: a corrupt double-clicked file must
                # not crash the server the analyst is waiting on.
                print(f"Skipped {os.path.basename(f)}: {e}", flush=True)
        for f in args.open_files:
            print(f"Importing {f} ...", flush=True)
            rec = STORE.ingest_csv(f, build_fts=not args.no_fts)
            print(f"  {rec['row_count']:,} rows in {rec['elapsed_sec']}s ({rec['rows_per_sec']:,}/s)")
            if rec.get("ragged_rows"):
                print(f"  {rec['ragged_rows']:,} row(s) had the wrong column count and were padded/trimmed to fit")
            if rec.get("suspect_quote_rows"):
                print(f"  {rec['suspect_quote_rows']:,} row(s) contain very long multi-line fields — "
                      "check for a stray quote if the row count looks low")
        print(f"Winnow on {url}  (case: {os.path.abspath(case_path)})")
    else:
        print(f"Winnow on {url}  (home screen — no case open)")
    if not args.no_browser:
        # Opened from a thread that waits for the port, rather than before
        # uvicorn.run() as this used to be — that raced, and an app window
        # landing on a connection error has no address bar to retry from.
        browser.open_when_ready(url, args.host, args.port,
                                app_mode=not args.browser_tab,
                                profile_dir=args.browser_profile)

    if args.no_idle_shutdown:
        PRESENCE.enabled = False

    # Registered before uvicorn blocks, not in the lifespan: the lifespan
    # doesn't know the port (uvicorn owns it by then), and a launcher that
    # probes an entry a moment before bind completes just gets a failed
    # probe and moves on — running() treats the file as a hint, the port as
    # the truth.
    with contextlib.suppress(Exception):
        instances.register(args.port, case_path if case_path else None)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
