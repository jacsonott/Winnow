"""Winnow's plugin host — Notepad++-style drop-in extensions.

A plugin is a folder (with an __init__.py) or a single .py file the analyst
drops into plugins/ next to server.py. On startup the server imports each
one and calls its register(api) function; the plugin uses the PluginAPI
object it's handed to add capabilities to the running app without touching
Winnow's own source. Deleting the folder removes the plugin — there is no
install step, no manifest database, nothing fetched from a network
(CLAUDE.md: assume airgapped).

The one extension point today is **ingest formats**: teach Winnow to read a
file format it doesn't natively understand (a raw $MFT, a USN journal, an
EVTX, a prefetch file, ...) by parsing it into rows that flow through the
same all-TEXT src_<id> path CSV/JSON imports use (Store.ingest_rows).
PluginAPI is deliberately an object rather than a bag of module functions —
it's the seam future hooks get added to (register_export_format,
register_command, ...) without any existing plugin needing to change.

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

# Bumped when PluginAPI's contract changes incompatibly. A plugin may
# declare WINNOW_API_VERSION = N (the version it was written against);
# loading refuses a plugin that asks for a newer API than this build
# provides, with a message that says to update Winnow — the failure mode
# is otherwise an AttributeError deep inside register() that reads like a
# plugin bug.
PLUGIN_API_VERSION = 1

FORMAT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
OPTION_TYPES = {"bool", "text", "choice"}


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

    def __init__(self, registry: "PluginRegistry", plugin_name: str):
        self._registry = registry
        self._plugin = plugin_name

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


class PluginRegistry:
    """Discovers, imports and indexes plugins. One module-level instance
    lives in server.py; tests build their own against tmp dirs.

    A plugin that fails — import error, missing register(), an exception
    inside register() — is recorded with its error and skipped; it never
    takes the server or the other plugins down with it. The error is
    surfaced in GET /api/plugins so the Plugins modal can show *why* a
    plugin didn't load instead of it silently not existing."""

    def __init__(self):
        self.plugins: list[dict] = []       # [{name, path, version, description, error|None, formats:[ids]}]
        self._formats: dict[str, IngestFormat] = {}
        self._seq = 0  # unique module names across load() calls / same-named plugins in two dirs

    # ------------------------------------------------------------- loading

    def load(self, directories: Iterable[str | Path]) -> None:
        """(Re)load from scratch. Directories that don't exist are fine —
        the default plugins/ dir simply not existing yet is the common
        fresh-checkout state, not an error."""
        self.plugins = []
        self._formats = {}
        for directory in directories:
            d = Path(directory)
            if not d.is_dir():
                continue
            for candidate in sorted(d.iterdir(), key=lambda p: p.name.lower()):
                if candidate.name.startswith((".", "_")):
                    continue
                if candidate.is_file() and candidate.suffix == ".py":
                    self._load_one(candidate.stem, candidate, candidate)
                elif candidate.is_dir() and (candidate / "__init__.py").is_file():
                    self._load_one(candidate.name, candidate / "__init__.py", candidate)

    def _load_one(self, default_name: str, entry: Path, root: Path) -> None:
        self._seq += 1
        # Unique, never-importable-by-accident module name; the seq keeps a
        # reload (or the same plugin name in two directories) from silently
        # reusing sys.modules state from a previous load.
        mod_name = f"winnow_plugin_{self._seq}_{default_name}"
        record = {
            "name": default_name, "path": str(root), "version": None,
            "description": "", "error": None, "formats": [],
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

            before = set(self._formats)
            register(PluginAPI(self, record["name"]))
            record["formats"] = sorted(set(self._formats) - before)
        except Exception as e:
            # Full traceback to the console for the plugin author; a
            # one-liner in the record for the UI.
            traceback.print_exc()
            record["error"] = f"{type(e).__name__}: {e}"

    def _add_format(self, fmt: IngestFormat) -> None:
        if fmt.id in self._formats:
            raise ValueError(f"Duplicate ingest format id: {fmt.id}")
        self._formats[fmt.id] = fmt

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

    def describe(self) -> list[dict]:
        return [dict(p) for p in self.plugins]
