"""Winnow — a local web app for reading very large CSVs out of SQLite.

    python server.py --case case.db --open sample.csv

Then browse to http://127.0.0.1:8777
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import workspace as WS
from store import SQLITE_IMPORT_EXTENSIONS, OpCancelled, Store

HERE = Path(__file__).parent
app = FastAPI(title="Winnow")
STORE: Store | None = None

# Directories the analyst has recently pointed the server at (folder/file
# browser, directory-import scans) — the freshest candidates for
# resolve_local's path recovery. In-process only, on purpose: it's a
# convenience cache, not state worth persisting.
RECENT_DIRS: collections.deque[str] = collections.deque(maxlen=20)


def _note_recent_dir(path: str) -> None:
    if path in RECENT_DIRS:
        RECENT_DIRS.remove(path)
    RECENT_DIRS.appendleft(path)


def _is_loopback(request: Request) -> bool:
    """Whether the TCP peer is this machine. request.client comes from the
    socket's peer address (uvicorn), not from anything the client sends, so
    it can't be header-spoofed. "testclient" is Starlette's TestClient peer
    name — never a real IP, so allowing it can't admit a network peer. The
    one honest caveat: an SSH-tunneled remote client also looks like
    loopback; resolve_local's full content fingerprint is what keeps that
    from ever importing the wrong file (see api_ingest_resolve_local)."""
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "testclient")

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
    if request.url.path.startswith("/static/") or request.url.path == "/":
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


class DirectoryScan(BaseModel):
    root: str
    recursive: bool = True
    extensions: list[str] | None = None
    include_patterns: list[str] = []
    exclude_patterns: list[str] = []


class ImportProfileWrite(BaseModel):
    id: int | None = None
    name: str
    extensions: list[str] | None = None
    include_patterns: list[str] = []
    exclude_patterns: list[str] = []
    recursive: bool = True


class CaseOpen(BaseModel):
    path: str


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
    return {"open": True, "path": STORE.path, "name": _case_display_name(STORE.path)}


@app.post("/api/case/open")
def api_case_open(body: CaseOpen):
    global STORE
    if not os.path.isfile(body.path):
        raise HTTPException(400, f"No case file at {body.path}")
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
    return {"sources": STORE.list_sources(), "name": rec["name"]}


@app.post("/api/case/compact")
def api_case_compact():
    """VACUUM the open case file. Long-running and explicitly analyst-
    triggered — see Store.compact for why it isn't automatic."""
    try:
        return store().compact()
    except ValueError as e:
        raise HTTPException(400, str(e))  # not enough free disk


@app.get("/api/browse_dir")
def api_browse_dir(path: str = ""):
    """Directory names only, one level — backs the "Browse..." folder picker
    in the new-case modal. A regular browser file/folder input can't hand
    back a real filesystem path (sandboxed for security), and this is a
    local, single-analyst tool with no auth already (see CLAUDE.md) — the
    plain-text case-path field already lets the analyst create a case file
    anywhere on disk they can type a path to, so this is a more convenient
    way to reach the same place, not a new capability. GET, like every
    other read-only route, so no CSRF header is required, but that's fine:
    a cross-origin page can still fire the request, it just can't read the
    response (no CORS headers on it), same as every other GET here."""
    base = os.path.abspath(os.path.expanduser(path)) if path else str(Path.home())
    if not os.path.isdir(base):
        raise HTTPException(400, f"Not a directory: {base}")
    _note_recent_dir(base)
    try:
        dirs = sorted(
            (e.name for e in os.scandir(base) if e.is_dir() and not e.name.startswith(".")),
            key=str.lower,
        )
    except PermissionError:
        dirs = []
    parent = os.path.dirname(base)
    return {"path": base, "parent": parent if parent != base else None, "dirs": dirs}


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
    _note_recent_dir(os.path.abspath(body.root))
    return store().scan_import_directory(
        body.root, recursive=body.recursive, extensions=body.extensions,
        include_patterns=body.include_patterns, exclude_patterns=body.exclude_patterns,
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


@app.post("/api/ingest/preview")
async def api_ingest_preview(
    file: UploadFile = File(...),
    delimiter: str | None = Form(None),
    has_header: bool = Form(True),
):
    raw = await file.read(512 * 1024)  # bounded sample — full parse happens at real ingest time
    text = raw.decode("utf-8-sig", errors="replace")
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
            text = raw.decode("utf-8-sig", errors="replace")
            return store().preview_csv_text(text, delimiter=body.delimiter or None,
                                            has_header=body.has_header)
        if kind == "json":
            return store().preview_json_file(body.path, flatten_mode=body.flatten_mode,
                                             flatten_depth=body.flatten_depth)
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


def _resolve_candidate_dirs() -> list[str]:
    """Likely homes for a just-picked file, cheapest-to-check first. Not a
    disk search — a fixed handful of stat calls: recently browsed/scanned
    dirs, the dirs previous imports came from (the DC-02-next-to-DC-01
    workflow), registered case files' dirs, and the standard user dirs a
    file dialog usually opens on."""
    dirs: list[str] = list(RECENT_DIRS)
    with contextlib.suppress(Exception):
        for src in store().list_sources():
            if src.get("path"):
                dirs.append(os.path.dirname(src["path"]))
    with contextlib.suppress(Exception):
        for case in WS.cases.list():
            if case.get("path"):
                dirs.append(os.path.dirname(case["path"]))
    home = Path.home()
    dirs += [str(home / d) for d in ("Downloads", "Desktop", "Documents")]
    dirs.append(str(home))
    seen: set[str] = set()
    out = []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


@app.post("/api/ingest/resolve_local")
async def api_ingest_resolve_local(
    request: Request,
    name: str = Form(...),
    size: int = Form(...),
    mtime_ms: int = Form(...),
    head: UploadFile = File(...),
    tail: UploadFile = File(...),
):
    """Path recovery for a same-host pick: the browser sandbox never
    reveals a picked file's real path, but if the client is this machine,
    the picked file is *somewhere* on this disk — so the frontend sends a
    fingerprint (name, size, mtime, first/last 64 KB) and this looks for
    it in _resolve_candidate_dirs. A hit means the import can read the
    file in place (jobs/path) instead of copying every byte through an
    upload; a miss just means the upload happens as before. The match is
    deliberately strict — exact name and size, mtime within 2s (FAT
    granularity), *and* byte-equal head+tail — so a tunneled client whose
    loopback appearance lies about locality still can't be handed the
    wrong file: matching bytes at both ends of an identically-sized,
    identically-stamped file is the same evidence either way."""
    if not _is_loopback(request):
        return {"path": None}
    head_bytes = await head.read(65536)
    tail_bytes = await tail.read(65536)
    base_name = os.path.basename(name)  # never let a crafted name walk dirs
    for d in _resolve_candidate_dirs():
        cand = os.path.join(d, base_name)
        try:
            st = os.stat(cand)
            if not os.path.isfile(cand) or st.st_size != size:
                continue
            if abs(st.st_mtime * 1000 - mtime_ms) > 2000:
                continue
            with open(cand, "rb") as f:
                if f.read(len(head_bytes)) != head_bytes:
                    continue
                f.seek(max(0, st.st_size - len(tail_bytes)))
                if f.read(len(tail_bytes)) != tail_bytes:
                    continue
            return {"path": cand}
        except OSError:
            continue
    return {"path": None}


@app.get("/api/ingest/jobs")
def api_ingest_jobs():
    return {"jobs": store().list_ingest_jobs()}


@app.post("/api/ingest/jobs/{job_id}/cancel")
def api_ingest_job_cancel(job_id: int):
    return {"cancelled": store().cancel_ingest_job(job_id)}


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
                       limit: int = 1000, path: str = "", op_token: str | None = None):
    try:
        # `path` (the outer levels already fixed by nested grouping) is a
        # JSON-encoded list — GET query params don't carry structured data,
        # and this stays a GET (not POST) so it keeps the same cacheable,
        # side-effect-free shape as every other view-summary read.
        path_list = json.loads(path) if path else None
        return JSONResponse(store().group_summary(view_id, column, order=order, direction=direction,
                                                  limit=min(limit, 5000), path=path_list,
                                                  op_token=op_token))
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


def main() -> None:
    global STORE
    ap = argparse.ArgumentParser(description="Winnow")
    ap.add_argument("--case", default=None, help="SQLite case file (created if missing). Omit to land on the home screen.")
    ap.add_argument("--open", dest="open_files", nargs="*", default=[], help="CSVs to ingest at startup")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--no-fts", action="store_true", help="Skip full-text index (faster import)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding to {args.host} instead of loopback. Winnow has no "
            "login and no access control — anyone who can reach this address on the "
            "network can open, import into, or query any case this server has access to. "
            "Only do this on a trusted, isolated network.",
            file=sys.stderr,
        )

    case_path = args.case or ("case.db" if args.open_files else None)
    url = f"http://{args.host}:{args.port}"
    if case_path:
        STORE = _open_store(case_path)
        legacy_presets = STORE.pop_legacy_presets()
        if legacy_presets:
            WS.filters.import_all({"filters": legacy_presets}, merge=True)
        WS.cases.create(case_path, name=os.path.splitext(os.path.basename(case_path))[0])
        WS.cases.touch_opened(WS.cases.find_by_path(case_path)["id"])
        for f in args.open_files:
            print(f"Importing {f} ...", flush=True)
            rec = STORE.ingest_csv(f, build_fts=not args.no_fts)
            print(f"  {rec['row_count']:,} rows in {rec['elapsed_sec']}s ({rec['rows_per_sec']:,}/s)")
            if rec.get("ragged_rows"):
                print(f"  {rec['ragged_rows']:,} row(s) had the wrong column count and were padded/trimmed to fit")
        print(f"Winnow on {url}  (case: {os.path.abspath(case_path)})")
    else:
        print(f"Winnow on {url}  (home screen — no case open)")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
