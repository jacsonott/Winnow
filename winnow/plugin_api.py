"""Winnow's plugin host — Notepad++-style drop-in extensions.

A plugin is a folder (with an __init__.py) or a single .py file the analyst
drops into plugins/ next to server.py. On startup the server imports each
one and calls its register(api) function; the plugin uses the PluginAPI
object it's handed to add capabilities to the running app without touching
Winnow's own source. Deleting the folder removes the plugin — there is no
install step, no manifest database, nothing fetched from a network
(CLAUDE.md: assume airgapped).

Three extension points today, all on the PluginAPI object a plugin's
register() receives (deliberately an object rather than a bag of module
functions — the seam future hooks get added to without any existing
plugin needing to change):

- **Ingest formats** (register_ingest_format): teach Winnow to read a
  file format it doesn't natively understand (a raw $MFT, a USN journal,
  an EVTX, a prefetch file, ...) by parsing it into rows that flow
  through the same all-TEXT src_<id> path CSV/JSON imports use
  (Store.ingest_rows).
- **Tabs** (register_tab): a pinned tab in the app — like the built-in
  SQL and Timeline tabs — whose content is entirely the plugin's own UI:
  an ES module the plugin ships, mounted into a container the app
  provides. This is how a plugin adds a whole feature surface (a lateral
  movement graph, an LLM assistant, a report builder, ...) rather than
  just a parser.
- **API routes** (register_api): backend endpoints under
  /api/plugin/<fs_name>/<route>, for whatever the plugin's UI (or a
  script) needs the server to do — query the case, call an external
  service, run a computation. Folder plugins only, same as tabs.

A plugin module provides:

    PLUGIN = {"name": "...", "version": "...", "description": "..."}   # optional
    WINNOW_API_VERSION = 1                                             # optional

    def register(api):                                                 # required
        api.register_ingest_format(
            id="mft",                        # unique within this plugin
            label="NTFS $MFT (raw)",         # what the import UI shows
            extensions=[".mft"],             # match by file extension...
            filename_patterns=["$MFT"],      # ...or by fnmatch on the bare name
            description="...",
            options=[                        # optional; rendered generically by the UI
                {"name": "records", "label": "Records", "type": "choice",
                 "choices": ["all", "in-use", "deleted"], "default": "all"},
            ],
            parse=my_parse,
        )

    def my_parse(path, options):
        # Streaming contract, mirroring ingest_csv's: columns are fixed up
        # front, rows is any iterable of sequences aligned to them (a
        # generator keeps memory flat on multi-GB inputs). Ragged rows get
        # padded/trimmed and counted, same as a ragged CSV line. Cells may
        # be str/int/None — Store.ingest_rows stringifies (TEXT columns,
        # same evidence-fidelity convention as every other ingest path).
        return {
            "columns": ["Timestamp", "FileName"],
            "rows": iter_rows(path),
            "column_types": ["datetime", "text"],   # optional; else sampled/inferred
            "name": "custom display name",          # optional; else the file's basename
        }

A tab plus its backend route, the full custom-UI shape:

    def register(api):
        api.register_tab(
            id="graph",                  # unique within this plugin
            label="Lateral movement",    # the pinned tab's caption
            entry="ui/tab.js",           # ES module, relative to the plugin folder,
        )                                #   served at /plugin_assets/<fs_name>/ui/tab.js
        api.register_api("edges", edges_handler, methods=["POST"])

    # ui/tab.js — mounted on first activation. `container` is an empty
    # <section> filling the main content area; `winnow` is the stable UI
    # context (see buildPluginTabContext in app.js): winnow.api/post/toast/
    # el/modal helpers, winnow.base ("/api/plugin/<fs_name>") for the
    # plugin's own routes, winnow.assets for its other files, winnow.sql()
    # for read-only case queries, winnow.schemaText() for an LLM-ready
    # schema dump, and winnow.state (live sources/tags/selection getters).
    # Optional exports: onShow/onHide, called on every tab switch.
    #
    #   export default function mount(container, winnow) { ... }

    def edges_handler(req):
        # req: PluginRequest — method, route, query (dict of str), body
        # (parsed JSON or None), and store (the open Store, or None when no
        # case is open). For reads, use req.store.run_sql(sql, limit) — it
        # opens its own read-only connection, so a slow plugin query never
        # holds the shared connection's lock (invariant #4). Raise
        # ValueError for a 400 the analyst can act on; return anything
        # JSON-able.
        if req.store is None:
            raise ValueError("Open a case first")
        return req.store.run_sql("SELECT ...", limit=5000)

SECURITY: a plugin is arbitrary Python running with Winnow's own
privileges — the same trust model as a Notepad++ plugin or an autopsy/
Ghidra script. Winnow only ever loads from local plugin directories the
analyst controls and never fetches code; the analyst placing a file there
*is* the consent step. Only install plugins you have read or trust.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Iterable

# store does not import this module, so this is not a cycle. It is here so
# PluginAPI can hand plugins the same quoting helper the app uses rather
# than having them import app internals themselves.
from .store import NUM_RE as _store_num_re, q as _store_q

# Bumped when PluginAPI's contract changes incompatibly. A plugin may
# declare WINNOW_API_VERSION = N (the version it was written against);
# loading refuses a plugin that asks for a newer API than this build
# provides, with a message that says to update Winnow — the failure mode
# is otherwise an AttributeError deep inside register() that reads like a
# plugin bug.
PLUGIN_API_VERSION = 2

FORMAT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# API routes may nest ("chat/stream") but each segment keeps the same shape.
ROUTE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$")
OPTION_TYPES = {"bool", "text", "choice"}
HTTP_METHODS = {"GET", "POST", "PUT", "DELETE"}


class PluginRequest:
    """What a register_api handler receives — a deliberately plain shape
    (no FastAPI types) so the contract stays stable across framework
    versions and handlers are trivially testable. `store` is the currently
    open Store, or None when no case is open."""

    def __init__(self, method: str, route: str, query: dict, body: Any, store: Any):
        self.method = method
        self.route = route
        self.query = query
        self.body = body
        self.store = store


class IngestFormat:
    """One registered way to turn a file into rows. `id` is namespaced as
    "<plugin>.<local id>" so two plugins can both call a format "mft"
    without colliding."""

    def __init__(self, *, plugin: str, local_id: str, label: str,
                 parse: Callable[[str, dict], dict],
                 extensions: Iterable[str] = (), filename_patterns: Iterable[str] = (),
                 description: str = "", options: Iterable[dict] = ()):
        self.plugin = plugin
        self.id = f"{plugin}.{local_id}"
        self.label = label
        self.parse = parse
        # Normalized like store.py's extension handling: leading dot, lowercase.
        self.extensions = [
            (e if e.startswith(".") else "." + e).lower() for e in extensions
        ]
        self.filename_patterns = [p.strip() for p in filename_patterns if p and p.strip()]
        self.description = description
        self.options = [dict(o) for o in options]

    def matches(self, filename: str) -> bool:
        """Extension or bare-filename fnmatch, case-insensitive — the same
        two match shapes scan_import_directory uses, because extension-less
        NTFS artifacts ("$MFT", "$J") are exactly what plugins exist to
        ingest and an extension list alone can never name them."""
        # Last path component across both separators — callers hand in
        # names from Windows-collected evidence and browser File objects,
        # and posix basename() leaves a backslash path whole.
        base = re.split(r"[\\/]", filename)[-1]
        ext = os.path.splitext(base)[1].lower()
        if ext and ext in self.extensions:
            return True
        low = base.lower()
        return any(fnmatch.fnmatch(low, p.lower()) for p in self.filename_patterns)

    def resolve_options(self, values: dict | None) -> dict:
        """Declared defaults overlaid with whatever the caller sent, with a
        ValueError (-> a 400, not a 500) for a choice value outside the
        declared set. Undeclared keys are dropped rather than passed
        through — the options dict comes from the browser."""
        out: dict[str, Any] = {}
        values = values or {}
        for opt in self.options:
            name = opt["name"]
            v = values.get(name, opt.get("default"))
            if opt.get("type") == "bool":
                v = bool(v)
            elif v is not None:
                v = str(v)
            if opt.get("type") == "choice" and v not in opt.get("choices", []):
                raise ValueError(f"Invalid value for option {name!r}: {v!r}")
            out[name] = v
        return out

    def describe(self) -> dict:
        return {
            "id": self.id,
            "plugin": self.plugin,
            "label": self.label,
            "extensions": self.extensions,
            "filename_patterns": self.filename_patterns,
            "description": self.description,
            "options": self.options,
        }


class PluginAPI:
    """What a plugin's register() receives. One instance per plugin, so
    registrations are attributed to the plugin that made them."""

    api_version = PLUGIN_API_VERSION

    #: SQL identifier quoting. Column and table names are user data — they
    #: come from CSV headers — so they are never f-stringed into SQL.
    #: Exposed here as of API version 2 so a plugin no longer has to reach
    #: into the app's own modules for it: plugins load by file path, not
    #: from the package, so `from store import q` only ever worked because
    #: the server happened to be started from the install root.
    q = staticmethod(_store_q)
    #: "Does this text look numeric" — the same test the grid's own
    #: right-alignment and numeric sorting use, so a plugin's tables agree
    #: with the rest of the app.
    NUM_RE = _store_num_re

    def __init__(self, registry: "PluginRegistry", plugin_name: str,
                 fs_name: str, root: Path):
        self._registry = registry
        self._plugin = plugin_name
        self._fs = fs_name
        self._root = root

    def register_ingest_format(self, *, id: str, label: str, parse: Callable,
                               extensions: Iterable[str] = (),
                               filename_patterns: Iterable[str] = (),
                               description: str = "",
                               options: Iterable[dict] = ()) -> None:
        if not FORMAT_ID_RE.match(id or ""):
            raise ValueError(f"Format id {id!r} must be lowercase [a-z0-9_-]")
        if not label or not callable(parse):
            raise ValueError("register_ingest_format needs a label and a callable parse")
        for opt in options:
            if not isinstance(opt, dict) or not opt.get("name"):
                raise ValueError("Each option must be a dict with a 'name'")
            if opt.get("type", "text") not in OPTION_TYPES:
                raise ValueError(f"Option {opt['name']!r}: type must be one of {sorted(OPTION_TYPES)}")
            if opt.get("type") == "choice" and not opt.get("choices"):
                raise ValueError(f"Option {opt['name']!r}: choice type needs 'choices'")
        fmt = IngestFormat(
            plugin=self._plugin, local_id=id, label=label, parse=parse,
            extensions=extensions, filename_patterns=filename_patterns,
            description=description, options=options,
        )
        self._registry._add_format(fmt)

    def register_tab(self, *, id: str, label: str, entry: str,
                     description: str = "") -> None:
        """A pinned tab (like the built-in SQL/Timeline tabs) whose content
        is the plugin's own UI. `entry` is an ES module path relative to
        the plugin folder — validated to exist *now*, at registration, so a
        typo'd path is a visible load error in Settings → Plugins instead
        of a silent 404 the first time someone clicks the tab."""
        if not FORMAT_ID_RE.match(id or ""):
            raise ValueError(f"Tab id {id!r} must be lowercase [a-z0-9_-]")
        if not label:
            raise ValueError("register_tab needs a label")
        if not self._root.is_dir():
            raise ValueError("register_tab is for folder plugins — a single .py file has no assets to serve")
        rel = Path(str(entry).replace("\\", "/"))
        if rel.is_absolute() or ".." in rel.parts or not (self._root / rel).is_file():
            raise ValueError(f"Tab entry {entry!r} must be a file inside the plugin folder")
        self._registry._add_tab({
            "id": f"{self._plugin}.{id}",
            "plugin": self._plugin,
            "plugin_fs": self._fs,
            "label": label,
            "entry": rel.as_posix(),
            "description": description,
        })

    def register_api(self, route: str, handler: Callable[[PluginRequest], Any],
                     methods: Iterable[str] = ("GET", "POST")) -> None:
        """A backend endpoint at /api/plugin/<fs_name>/<route>, dispatched
        to `handler(PluginRequest) -> JSON-able`. Raising ValueError in the
        handler becomes a 400 with the message; anything else is a 500.
        Non-GET calls get the same CSRF-header gate as every other /api/*
        route, for free, from server.py's middleware."""
        if not ROUTE_RE.match(route or ""):
            raise ValueError(f"Route {route!r} must be lowercase [a-z0-9_-] segments separated by /")
        if not callable(handler):
            raise ValueError("register_api needs a callable handler")
        methods = {str(m).upper() for m in methods}
        if not methods or methods - HTTP_METHODS:
            raise ValueError(f"methods must be a subset of {sorted(HTTP_METHODS)}")
        self._registry._add_api(self._fs, route, handler, methods)


class PluginRegistry:
    """Discovers, imports and indexes plugins. One module-level instance
    lives in server.py; tests build their own against tmp dirs.

    A plugin that fails — import error, missing register(), an exception
    inside register() — is recorded with its error and skipped; it never
    takes the server or the other plugins down with it. The error is
    surfaced in GET /api/plugins so the Plugins modal can show *why* a
    plugin didn't load instead of it silently not existing."""

    def __init__(self):
        self.plugins: list[dict] = []       # [{name, fs_name, path, version, description, error|None, enabled, gen, formats:[ids], tabs:[ids]}]
        self._formats: dict[str, IngestFormat] = {}
        self._tabs: dict[str, dict] = {}                 # namespaced tab id -> tab dict (see PluginAPI.register_tab)
        self._apis: dict[tuple[str, str], dict] = {}     # (fs_name, route) -> {handler, methods}
        self._seq = 0  # unique module names across load() calls / same-named plugins in two dirs

    # ------------------------------------------------------------- loading

    def load(self, directories: Iterable[str | Path], disabled: Iterable[str] = (),
             enabled_for=None, bundled_dirs: Iterable[str | Path] = ()) -> None:
        """(Re)load from scratch. Directories that don't exist are fine —
        the default plugins/ dir simply not existing yet is the common
        fresh-checkout state, not an error.

        Safe to call again on a live server — Settings → Plugins toggles
        and installs do exactly that instead of requiring a restart. The
        registry is rebuilt wholesale; modules from earlier loads linger
        in sys.modules under their unique per-load names (Python can't
        truly unload code) but nothing references them again.

        A plugin the policy turns off is *discovered but never imported* —
        it appears in the listing with enabled=False so the UI can offer
        the switch, and its code never runs, which is the entire value of
        an off switch on something that executes with the app's
        privileges. The policy itself is the caller's: `enabled_for(fs_name,
        directory) -> bool` (server.py folds the machine-level prefs and
        the open case's overrides into one closure there). The plain
        `disabled` set stays as the simple form of the same thing.

        The same fs_name in two directories loads once, from the earliest
        directory — that's what makes the bundled-examples dir safe to
        append after plugins/: an analyst's installed (possibly edited)
        copy of an example shadows the shipped one instead of both
        loading and fighting over tab ids and routes.

        `bundled_dirs` entries mark their plugins `bundled: True` in the
        listing — presentation only ("ships with Winnow" in the panel);
        enablement policy is entirely `enabled_for`'s business."""
        disabled = set(disabled)
        if enabled_for is None:
            enabled_for = lambda fs_name, directory: fs_name not in disabled  # noqa: E731
        bundled = {str(Path(b)) for b in bundled_dirs}
        self.plugins = []
        self._formats = {}
        self._tabs = {}
        self._apis = {}
        seen: set[str] = set()
        for directory in directories:
            d = Path(directory)
            if not d.is_dir():
                continue
            is_bundled = str(d) in bundled
            for candidate in sorted(d.iterdir(), key=lambda p: p.name.lower()):
                if candidate.name.startswith((".", "_")):
                    continue
                if candidate.is_file() and candidate.suffix == ".py":
                    fs_name, entry = candidate.stem, candidate
                elif candidate.is_dir() and (candidate / "__init__.py").is_file():
                    fs_name, entry = candidate.name, candidate / "__init__.py"
                else:
                    continue
                if fs_name in seen:
                    continue
                seen.add(fs_name)
                if not enabled_for(fs_name, str(d)):
                    self.plugins.append({
                        "name": fs_name, "fs_name": fs_name, "path": str(candidate),
                        "version": None, "description": "", "error": None,
                        "enabled": False, "gen": 0, "formats": [], "tabs": [],
                        "bundled": is_bundled,
                    })
                else:
                    self._load_one(fs_name, entry, candidate, bundled=is_bundled)

    def _load_one(self, default_name: str, entry: Path, root: Path, bundled: bool = False) -> None:
        self._seq += 1
        # Unique, never-importable-by-accident module name; the seq keeps a
        # reload (or the same plugin name in two directories) from silently
        # reusing sys.modules state from a previous load.
        mod_name = f"winnow_plugin_{self._seq}_{default_name}"
        record = {
            "name": default_name, "fs_name": default_name, "path": str(root),
            "version": None, "description": "", "error": None,
            # gen = this load's sequence number — the frontend cache-busts
            # a tab entry's import() URL with it, so a toggle-off/on (or
            # any registry reload) picks up changed JS instead of the
            # browser's cached module.
            "enabled": True, "gen": self._seq, "formats": [], "tabs": [],
            "bundled": bundled,
        }
        self.plugins.append(record)
        try:
            spec = importlib.util.spec_from_file_location(
                mod_name, entry,
                # A folder plugin is a real package, so `from . import x`
                # works inside it and it can ship helper modules.
                submodule_search_locations=[str(root)] if entry.name == "__init__.py" else None,
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)

            meta = getattr(module, "PLUGIN", None) or {}
            record["name"] = str(meta.get("name") or default_name)
            record["version"] = str(meta["version"]) if meta.get("version") else None
            record["description"] = str(meta.get("description") or "")

            wants = getattr(module, "WINNOW_API_VERSION", None)
            if wants is not None and int(wants) > PLUGIN_API_VERSION:
                raise RuntimeError(
                    f"needs Winnow plugin API v{wants}, this build provides v{PLUGIN_API_VERSION} — update Winnow"
                )
            register = getattr(module, "register", None)
            if not callable(register):
                raise RuntimeError("plugin has no register(api) function")

            before_formats = set(self._formats)
            before_tabs = set(self._tabs)
            register(PluginAPI(self, record["name"], record["fs_name"], root))
            record["formats"] = sorted(set(self._formats) - before_formats)
            record["tabs"] = sorted(set(self._tabs) - before_tabs)
        except Exception as e:
            # Full traceback to the console for the plugin author; a
            # one-liner in the record for the UI.
            traceback.print_exc()
            record["error"] = f"{type(e).__name__}: {e}"

    def _add_format(self, fmt: IngestFormat) -> None:
        if fmt.id in self._formats:
            raise ValueError(f"Duplicate ingest format id: {fmt.id}")
        self._formats[fmt.id] = fmt

    def _add_tab(self, tab: dict) -> None:
        if tab["id"] in self._tabs:
            raise ValueError(f"Duplicate tab id: {tab['id']}")
        self._tabs[tab["id"]] = tab

    def _add_api(self, fs_name: str, route: str, handler: Callable, methods: set[str]) -> None:
        if (fs_name, route) in self._apis:
            raise ValueError(f"Duplicate API route: {route}")
        self._apis[(fs_name, route)] = {"handler": handler, "methods": methods}

    # -------------------------------------------------------------- lookup

    def get_format(self, format_id: str) -> IngestFormat:
        if format_id not in self._formats:
            raise KeyError(f"No such ingest format: {format_id}")
        return self._formats[format_id]

    def format_for_filename(self, filename: str) -> IngestFormat | None:
        """First registered format claiming this filename, in plugin load
        order — used as the default routing for dropped/scanned files. The
        analyst can always pick a different format explicitly."""
        for fmt in self._formats.values():
            if fmt.matches(filename):
                return fmt
        return None

    # ------------------------------------------------------------ describe

    def list_formats(self) -> list[dict]:
        return [f.describe() for f in self._formats.values()]

    def list_tabs(self) -> list[dict]:
        """Every registered tab, each carrying its plugin's gen so the
        frontend can cache-bust the entry module URL per load."""
        gen_by_fs = {p["fs_name"]: p["gen"] for p in self.plugins}
        return [{**t, "gen": gen_by_fs.get(t["plugin_fs"], 0)} for t in self._tabs.values()]

    def get_api(self, fs_name: str, route: str) -> dict | None:
        return self._apis.get((fs_name, route))

    def describe(self) -> list[dict]:
        return [dict(p) for p in self.plugins]
