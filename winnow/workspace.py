"""Cross-case application state, stored as human-readable JSON in
workspace/ under the install root (paths.INSTALL_ROOT) — independent of
any single case.db, so it survives switching between cases and travels
with the app folder (airgapped/portable, same spirit as CLAUDE.md's
no-CDN rule).

Eleven small stores, each backed by its own JSON file under workspace/.
Adding one means adding it here too — this list read "eight" for three
stores longer than it was true:
  cases.json              the home screen's case registry
  filters.json            saved/cyclable filters ([ and ] in the grid) — also
                           the one mechanism behind the "suggested filter"
                           banner a matching header set gets on open; there's
                           no separate case-scoped "preset" concept anymore
                           (see CLAUDE.md)
  header_nicknames.json   friendly names for a header set, e.g. "EVTX
                           exports" instead of a long raw column list —
                           shown wherever a saved filter's header set would
                           otherwise print in full
  timeline_templates.json the "which column is the timestamp, which columns
                           make up the body, what do we call this source
                           type" template a header set uses in the unified
                           Timeline tab — see CLAUDE.md
  tags.json               the default tag template used to seed brand-new cases
  column_layouts.json     default column order/visibility per header set
  import_profiles.json    named include/exclude glob patterns for directory
                           import (e.g. a "KAPE" profile excluding known-noisy
                           CSVs) — reusable across cases, same reason
                           timeline_templates.json and column_layouts.json are:
                           configure once, every future case with a similar
                           collection benefits
  plugins.json            which installed plugins are toggled off (Settings →
                           Plugins). Machine-level workflow state, not part of
                           any case — and deliberately a *disabled* list, so a
                           freshly dropped-in plugin is on by default and
                           deleting workspace/ re-enables everything rather
                           than silently turning it all off
  plugin_bundles.json     named sets of plugins ("case types") applied
                           together from Settings → Plugins
  app_settings.json       app-wide display preferences, e.g. the default
                           timestamp format for cases that don't set their
                           own — a case's own choice lives in the case
                           file's case_settings, so it travels with the case
  prefs.json              per-INSTALL machine preferences, today just
                           cases_dir: where new case files go, asked once on
                           first run rather than silently defaulting

Never holds evidence data — only UI/workflow bookkeeping. Analyst work
(tags, notes, layouts, saved views) lives in the case file instead, so it
travels with the evidence; that split is deliberate.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import paths
from .store import DEFAULT_TAGS

WORKSPACE_DIR = paths.INSTALL_ROOT / "workspace"

_LOCK = threading.RLock()


def _ensure_dir() -> Path:
    WORKSPACE_DIR.mkdir(exist_ok=True)
    return WORKSPACE_DIR


def _read(name: str, default: Any) -> Any:
    path = _ensure_dir() / name
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write(name: str, data: Any) -> None:
    path = _ensure_dir() / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _next_id(items: list[dict]) -> int:
    return max((i["id"] for i in items), default=0) + 1


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class CaseRegistry:
    """The home screen's list of known case.db files — name/group/notes are
    display metadata only, never mutate the underlying case file."""

    FILE = "cases.json"

    def _load(self) -> list[dict]:
        return _read(self.FILE, {"cases": []})["cases"]

    def _save(self, items: list[dict]) -> None:
        _write(self.FILE, {"cases": items})

    def list(self) -> list[dict]:
        with _LOCK:
            return self._load()

    def get(self, case_id: int) -> dict | None:
        with _LOCK:
            return next((c for c in self._load() if c["id"] == case_id), None)

    def find_by_path(self, path: str) -> dict | None:
        abspath = os.path.abspath(path)
        with _LOCK:
            return next((c for c in self._load() if os.path.abspath(c["path"]) == abspath), None)

    def create(self, path: str, name: str, group: str = "", notes: str = "") -> dict:
        with _LOCK:
            items = self._load()
            existing = next(
                (c for c in items if os.path.abspath(c["path"]) == os.path.abspath(path)), None
            )
            if existing:
                return existing
            rec = {
                "id": _next_id(items),
                "path": os.path.abspath(path),
                "name": name,
                "group": group,
                "notes": notes,
                "created_at": _now(),
                "last_opened": None,
            }
            items.append(rec)
            self._save(items)
            return rec

    def update(self, case_id: int, **fields) -> dict:
        with _LOCK:
            items = self._load()
            rec = next((c for c in items if c["id"] == case_id), None)
            if not rec:
                raise KeyError(f"No case {case_id}")
            for k, v in fields.items():
                if v is not None:
                    rec[k] = v
            self._save(items)
            return rec

    def touch_opened(self, case_id: int) -> None:
        with _LOCK:
            items = self._load()
            for c in items:
                if c["id"] == case_id:
                    c["last_opened"] = _now()
            self._save(items)

    def delete(self, case_id: int) -> None:
        with _LOCK:
            items = [c for c in self._load() if c["id"] != case_id]
            self._save(items)


class SavedFilters:
    """Cyclable, named filters — [ and ] in the grid. Every filter carries
    the column-name set it was built for (col_names), which is also what
    powers the "suggested filter" banner a table gets on open when its
    headers match (exact) or nearly match (similar) a saved filter — a
    "preset" isn't a separate stored thing, just a saved filter whose
    col_names happens to match what's open right now."""

    FILE = "filters.json"

    def _load(self) -> list[dict]:
        return _read(self.FILE, {"filters": []})["filters"]

    def _save(self, items: list[dict], seeded_version: int | None = None) -> None:
        data = _read(self.FILE, {"filters": []})
        data["filters"] = items
        if seeded_version is not None:
            data["seeded_version"] = seeded_version
        _write(self.FILE, data)

    def ensure_seeded(self) -> None:
        """Merges the shipped triage filters (defaults/filters.json — the converted
        Timeline Explorer set — EVTX/Registry/MFT) into this store, once per
        FILTER_DEFAULTS_VERSION. Same contract as
        HeaderNicknames.ensure_seeded, for the same reasons: lazy (called
        from the read path so a test's WORKSPACE_DIR monkeypatch is
        honored), and seeded rows become ordinary records afterward —
        rename, edit, reorder, delete all stick. Identity for "already
        present" is name + column set, matching import_all's merge rule, so
        an analyst's edited copy of a default is never re-added beside
        itself on a version bump."""
        from . import defaults

        with _LOCK:
            data = _read(self.FILE, {"filters": []})
            shipped = defaults.filters()
            if data.get("seeded_version", 0) >= shipped["version"]:
                return
            items = data["filters"]
            present = {(r["name"], tuple(r["col_names"])) for r in items}
            for name, cols, payload in shipped["filters"]:
                if (name, tuple(cols)) in present:
                    continue
                items.append({"id": _next_id(items), "name": name,
                              "col_names": list(cols), "payload": payload,
                              "created_at": _now()})
            self._save(items, seeded_version=shipped["version"])

    @staticmethod
    def _normalize_payload(rec: dict) -> bool:
        """Wraps a bare-condition filter_tree root in an AND group, in
        place. Version 1 of the seeded defaults shipped several single-
        condition filters with the condition AS the root — a shape the
        server compiles fine but the client's spec gate read as "no
        filter", so they showed as applied while filtering nothing."""
        tree = (rec.get("payload") or {}).get("filter_tree")
        if isinstance(tree, dict) and tree.get("type") == "cond":
            rec["payload"]["filter_tree"] = {"type": "group", "op": "AND", "children": [tree]}
            return True
        return False

    def list(self) -> list[dict]:
        self.ensure_seeded()
        with _LOCK:
            items = self._load()
            if any([self._normalize_payload(r) for r in items]):
                self._save(items)
            return items

    def create(self, name: str, col_names: list[str], payload: dict) -> dict:
        with _LOCK:
            items = self._load()
            rec = {
                "id": _next_id(items),
                "name": name,
                "col_names": col_names,
                "payload": payload,
                "created_at": _now(),
            }
            items.append(rec)
            self._save(items)
            return rec

    def update(self, filter_id: int, *, name: str | None = None,
               col_names: list[str] | None = None, payload: dict | None = None) -> dict:
        """Partial update — only the fields actually passed are touched
        (same None-means-leave-alone convention as CaseRegistry.update), so
        this serves both a bare rename and a full re-save of the filter's
        conditions from the Filter builder's "Update" button.

        col_names is settable but the UI deliberately doesn't send it when
        re-saving an edited filter: the header set is what the [ / ] cycle
        order and the suggested-filter banner key off, so quietly rebinding
        a filter to whatever table happened to be open during the edit
        would move it out of the group it was saved for. "Save filter…"
        (create) is the path that binds to the open table's columns."""
        with _LOCK:
            items = self._load()
            rec = next((f for f in items if f["id"] == filter_id), None)
            if not rec:
                raise KeyError(f"No saved filter {filter_id}")
            for k, v in (("name", name), ("col_names", col_names), ("payload", payload)):
                if v is not None:
                    rec[k] = v
            self._save(items)
            return rec

    def delete(self, filter_id: int) -> None:
        with _LOCK:
            items = [f for f in self._load() if f["id"] != filter_id]
            self._save(items)

    def reorder(self, ordered_ids: list[int]) -> list[dict]:
        """Rearranges just the given ids into the given order, leaving
        every other (unlisted) filter exactly where it already was — so
        reordering one header set's filters (the cyclable [ / ] order,
        and the Saved Filters list) never disturbs another header set's
        relative order, even though both live in the same flat list.
        Works by taking the positions currently occupied by `ordered_ids`
        (in list order) and dropping the new sequence into those same
        slots, rather than moving the ids themselves to new positions."""
        with _LOCK:
            items = self._load()
            by_id = {f["id"]: f for f in items}
            id_set = set(ordered_ids)
            positions = [i for i, f in enumerate(items) if f["id"] in id_set]
            new_seq = [by_id[i] for i in ordered_ids if i in by_id]
            for pos, item in zip(positions, new_seq):
                items[pos] = item
            self._save(items)
            return items

    def export_all(self) -> dict:
        with _LOCK:
            return {"format": "winnow-filters/1", "filters": self._load()}

    def import_all(self, data: dict, merge: bool = True) -> int:
        incoming = data.get("filters", [])
        with _LOCK:
            items = self._load() if merge else []
            seen = {(f["name"], tuple(sorted(c.lower() for c in f["col_names"]))) for f in items}
            added = 0
            for f in incoming:
                key = (f.get("name"), tuple(sorted(c.lower() for c in f.get("col_names", []))))
                if key in seen:
                    continue
                rec = {
                    "id": _next_id(items),
                    "name": f.get("name", "Imported filter"),
                    "col_names": f.get("col_names", []),
                    "payload": f.get("payload", {}),
                    "created_at": _now(),
                }
                items.append(rec)
                seen.add(key)
                added += 1
            self._save(items)
            return added


class HeaderNicknames:
    """A friendly name for a header set (e.g. "EVTX exports" instead of a
    long raw column list) — purely a display label everywhere a saved
    filter's col_names would otherwise print in full. Keyed by the header
    set itself, like ColumnLayouts: saving again for the same set
    overwrites in place rather than accumulating duplicates, since two
    different nicknames for the exact same columns would just be
    confusing. Deliberately its own tiny store rather than a field on
    SavedFilters — several saved filters commonly share one header set
    and should all pick up the same nickname."""

    FILE = "header_nicknames.json"

    def _load(self) -> list[dict]:
        return _read(self.FILE, {"nicknames": []})["nicknames"]

    def _save(self, items: list[dict], seeded_version: int | None = None) -> None:
        data = _read(self.FILE, {"nicknames": []})
        data["nicknames"] = items
        if seeded_version is not None:
            data["seeded_version"] = seeded_version
        _write(self.FILE, data)

    def ensure_seeded(self) -> None:
        """Merges the shipped nicknames (defaults/headers.json — EvtxECmd, MFTECmd,
        Amcache, ... — see that module) into this store, once per
        DEFAULTS_VERSION. Called lazily from the read paths rather than at
        server import: the store writes to WORKSPACE_DIR, and at import
        time that's the developer's real workspace even when a test has a
        per-test dir waiting to be monkeypatched in.

        Seeded records become ordinary rows — rename and delete stick,
        because after seeding nothing distinguishes them from records the
        analyst created. Only header sets not already present are added,
        so an analyst's own name for the EvtxECmd shape survives every
        version bump."""
        from . import defaults

        with _LOCK:
            data = _read(self.FILE, {"nicknames": []})
            shipped = defaults.headers()
            if data.get("seeded_version", 0) >= shipped["version"]:
                return
            items = data["nicknames"]
            present = {tuple(r["col_names"]) for r in items}
            for nickname, cols in shipped["nicknames"]:
                key = self._key(cols)
                if tuple(key) in present:
                    continue
                items.append({"id": _next_id(items), "col_names": key, "nickname": nickname})
                present.add(tuple(key))
            self._save(items, seeded_version=shipped["version"])

    @staticmethod
    def _key(col_names: list[str]) -> list[str]:
        return sorted(c.strip().lower() for c in col_names)

    def list(self) -> list[dict]:
        self.ensure_seeded()
        with _LOCK:
            return self._load()

    def find(self, col_names: list[str]) -> dict | None:
        self.ensure_seeded()
        key = self._key(col_names)
        with _LOCK:
            for rec in self._load():
                if rec["col_names"] == key:
                    return rec
        return None

    def save(self, col_names: list[str], nickname: str) -> dict:
        key = self._key(col_names)
        with _LOCK:
            items = self._load()
            rec = next((r for r in items if r["col_names"] == key), None)
            if rec:
                rec["nickname"] = nickname
            else:
                rec = {"id": _next_id(items), "col_names": key, "nickname": nickname}
                items.append(rec)
            self._save(items)
            return rec

    def delete(self, nickname_id: int) -> None:
        with _LOCK:
            items = [r for r in self._load() if r["id"] != nickname_id]
            self._save(items)


class TimelineTemplates:
    """The "database of headers" the unified Timeline tab uses to turn a
    real source's raw columns into one timeline event per tagged row:
    which column is the timestamp, which columns (in the order given)
    get joined with " | " into the body, and what to call this source
    type (e.g. "Windows Event Log" instead of the raw file name). Keyed
    by header set like ColumnLayouts/HeaderNicknames — cross-case on
    purpose, so configuring a Chromium History import's columns once
    means every future case with a same-shaped History table already
    knows how to place it on a timeline. A source whose header set has no
    matching template here falls back to sensible defaults (first
    datetime column, every column, its own file name) — see server.py's
    _resolve_timeline_configs — so the Timeline tab is never blocked on
    setup, just improved by it."""

    FILE = "timeline_templates.json"

    def _load(self) -> list[dict]:
        return _read(self.FILE, {"templates": []})["templates"]

    def _save(self, items: list[dict]) -> None:
        _write(self.FILE, {"templates": items})

    @staticmethod
    def _key(col_names: list[str]) -> list[str]:
        return sorted(c.strip().lower() for c in col_names)

    def list(self) -> list[dict]:
        with _LOCK:
            return self._load()

    def find(self, col_names: list[str]) -> dict | None:
        key = self._key(col_names)
        with _LOCK:
            for rec in self._load():
                if rec["col_names"] == key:
                    return rec
        return None

    def save(
        self, col_names: list[str], type_label: str,
        timestamp_column: str | None, body_columns: list[str],
    ) -> dict:
        key = self._key(col_names)
        with _LOCK:
            items = self._load()
            rec = next((r for r in items if r["col_names"] == key), None)
            if rec:
                rec.update(type_label=type_label, timestamp_column=timestamp_column, body_columns=body_columns)
            else:
                rec = {
                    "id": _next_id(items), "col_names": key, "type_label": type_label,
                    "timestamp_column": timestamp_column, "body_columns": body_columns,
                }
                items.append(rec)
            self._save(items)
            return rec

    def delete(self, template_id: int) -> None:
        with _LOCK:
            items = [r for r in self._load() if r["id"] != template_id]
            self._save(items)


class ColumnLayouts:
    """The analyst's preferred default column order/visibility/timestamp
    format for a given header set — seeded onto a freshly-imported source
    (one that's never had its own /api/layout saved yet) when its column
    names match a set saved here. Keyed by the column-name set itself
    rather than named like SavedFilters: this is "the" default for that
    header shape, so saving again for the same set overwrites in place
    instead of accumulating duplicates."""

    FILE = "column_layouts.json"

    def _load(self) -> list[dict]:
        return _read(self.FILE, {"layouts": []})["layouts"]

    def _save(self, items: list[dict]) -> None:
        _write(self.FILE, {"layouts": items})

    @staticmethod
    def _key(col_names: list[str]) -> list[str]:
        return sorted(c.lower() for c in col_names)

    def find(self, col_names: list[str]) -> dict | None:
        key = self._key(col_names)
        with _LOCK:
            for rec in self._load():
                if rec["col_names"] == key:
                    return rec
        return None

    def save(self, col_names: list[str], order: list[str], columns: dict) -> dict:
        key = self._key(col_names)
        with _LOCK:
            items = self._load()
            rec = next((r for r in items if r["col_names"] == key), None)
            if rec:
                rec["order"] = order
                rec["columns"] = columns
                rec["saved_at"] = _now()
            else:
                rec = {"id": _next_id(items), "col_names": key, "order": order, "columns": columns, "saved_at": _now()}
                items.append(rec)
            self._save(items)
            return rec


class TagTemplate:
    """The default tag set applied when a brand-new case.db is created.
    Seeds itself from store.DEFAULT_TAGS on first read, so behavior is
    unchanged until the user edits it in Settings."""

    FILE = "tags.json"

    # What the pre-2026-08 seed wrote (Benign first). A template that still
    # reads exactly like this was never touched by the analyst, so it's safe
    # to migrate to the current DEFAULT_TAGS order (TA first) — any edit at
    # all (rename, recolor, rehotkey, reorder, add, remove) and it's theirs,
    # and get() leaves it alone forever.
    _LEGACY_SEED = [
        {"name": "Benign", "color": "#5d8a66", "hotkey": "1"},
        {"name": "Suspicious", "color": "#d68a2e", "hotkey": "2"},
        {"name": "TA", "color": "#c0392b", "hotkey": "3"},
    ]

    def get(self) -> list[dict]:
        with _LOCK:
            path = _ensure_dir() / self.FILE
            if not path.exists():
                initial = [{"name": n, "color": c, "hotkey": h} for n, c, h in DEFAULT_TAGS]
                self._save(initial)
                return initial
            current = _read(self.FILE, [])
            if current == self._LEGACY_SEED:
                migrated = [{"name": n, "color": c, "hotkey": h} for n, c, h in DEFAULT_TAGS]
                self._save(migrated)
                return migrated
            return current

    def _save(self, tags: list[dict]) -> None:
        _write(self.FILE, tags)

    def save(self, tags: list[dict]) -> list[dict]:
        with _LOCK:
            clean = [
                {"name": t.get("name", ""), "color": t.get("color", "#8899aa"), "hotkey": t.get("hotkey") or None}
                for t in tags
                if t.get("name", "").strip()
            ]
            self._save(clean)
            return clean

    def as_tuples(self) -> list[tuple]:
        return [(t["name"], t["color"], t.get("hotkey")) for t in self.get()]


class ImportProfiles:
    """Named include/exclude glob patterns for directory import
    (server.py's /api/ingest/dir/scan, Store.scan_import_directory) — e.g.
    a "KAPE" profile that excludes `*_Amcache_UnassociatedFileEntries.csv`
    (only lists executables that shipped with Windows, not triage signal).
    Cross-case on purpose, same reasoning as timeline_templates.json: build
    a profile once, every future triage of the same shape reuses it.

    Named records with an explicit id, like tags — not keyed by an implicit
    natural key the way TimelineTemplates/ColumnLayouts are keyed by header
    set, since there's no equivalent "shape" to key a directory-import
    profile on before anything's been scanned yet."""

    FILE = "import_profiles.json"

    def _load(self) -> list[dict]:
        return _read(self.FILE, {"profiles": []})["profiles"]

    def _save(self, items: list[dict]) -> None:
        _write(self.FILE, {"profiles": items})

    def list(self) -> list[dict]:
        with _LOCK:
            return self._load()

    def upsert(
        self, profile_id: int | None, name: str, extensions: list[str] | None,
        include_patterns: list[str], exclude_patterns: list[str], recursive: bool,
    ) -> dict:
        with _LOCK:
            items = self._load()
            rec = next((p for p in items if p["id"] == profile_id), None) if profile_id else None
            if rec:
                rec.update(
                    name=name, extensions=extensions, include_patterns=include_patterns,
                    exclude_patterns=exclude_patterns, recursive=recursive,
                )
            else:
                rec = {
                    "id": _next_id(items), "name": name, "extensions": extensions,
                    "include_patterns": include_patterns, "exclude_patterns": exclude_patterns,
                    "recursive": recursive, "created_at": _now(),
                }
                items.append(rec)
            self._save(items)
            return rec

    def delete(self, profile_id: int) -> None:
        with _LOCK:
            items = [p for p in self._load() if p["id"] != profile_id]
            self._save(items)


class PluginPrefs:
    """Which plugins are toggled off in Settings → Plugins, keyed by their
    filesystem name (the plugins/ entry's file/folder name — the only
    identity that exists *without* importing the plugin, which is the
    whole point: a disabled plugin's code never runs). Stored as a
    disabled list so presence in plugins/ means enabled by default."""

    FILE = "plugins.json"

    def _data(self) -> dict:
        d = _read(self.FILE, {"disabled": []})
        d.setdefault("disabled", [])
        d.setdefault("enabled_bundled", [])
        return d

    def disabled(self) -> set[str]:
        with _LOCK:
            return set(self._data()["disabled"])

    def enabled_bundled(self) -> set[str]:
        """Bundled examples run the default the other way — present but OFF
        until asked for (claude_assistant needs network + an API key, and
        even the airgap-safe ones are an explicit choice) — so their
        machine-level state is an *enabled* list, where installed plugins
        keep the disabled list. One file, two lists, one question:
        machine_enabled()."""
        with _LOCK:
            return set(self._data()["enabled_bundled"])

    def machine_enabled(self, fs_name: str, default_on: bool) -> bool:
        with _LOCK:
            d = self._data()
            if default_on:
                return fs_name not in d["disabled"]
            return fs_name in d["enabled_bundled"]

    def set_machine_enabled(self, fs_name: str, enabled: bool, default_on: bool) -> None:
        with _LOCK:
            d = self._data()
            key = "disabled" if default_on else "enabled_bundled"
            names = set(d[key])
            # membership means "off" for the disabled list, "on" for the
            # bundled-enabled list — the write inverts accordingly.
            wanted_in_list = (not enabled) if default_on else enabled
            (names.add if wanted_in_list else names.discard)(fs_name)
            d[key] = sorted(names)
            _write(self.FILE, d)

    def set_enabled(self, fs_name: str, enabled: bool) -> set[str]:
        """Compat shim for the pre-scopes call shape (installed plugins
        only)."""
        self.set_machine_enabled(fs_name, enabled, default_on=True)
        return self.disabled()


class AppSettings:
    """System-wide UI preferences that belong on the machine rather than in
    any one case file — currently just the default timestamp display
    format, which an analyst sets once and expects to apply to every case
    they open afterward.

    Not localStorage (like theme/keymap) because this is a workflow
    default, not a per-browser look; not the case file because it must
    apply to cases that don't exist yet. Same shape as TagTemplate: one
    small document, read whole, written whole."""

    FILE = "app_settings.json"
    DEFAULTS = {"default_ts_format": "iso"}
    # Mirrors app.js's TS_FORMATS. A value outside this set would silently
    # fall through formatTimestamp's switch and render raw, so it's
    # rejected here rather than stored and quietly ignored.
    TS_FORMATS = {"raw", "iso", "iso_ms", "iso_us", "date", "time", "us", "us_date"}

    def get(self) -> dict:
        with _LOCK:
            return {**self.DEFAULTS, **_read(self.FILE, {})}

    def save(self, values: dict) -> dict:
        fmt = values.get("default_ts_format")
        if fmt is not None and fmt not in self.TS_FORMATS:
            raise ValueError(f"Unknown timestamp format: {fmt}")
        with _LOCK:
            current = {**self.DEFAULTS, **_read(self.FILE, {})}
            if fmt is not None:
                current["default_ts_format"] = fmt
            _write(self.FILE, current)
            return current


cases = CaseRegistry()
class MachinePrefs:
    """Small per-machine app preferences that belong to the INSTALL, not a
    case: today just cases_dir — where new case files go, asked once on
    first run instead of silently defaulting to ./cases."""

    FILE = "prefs.json"

    def get(self, key: str, default=None):
        with _LOCK:
            return _read(self.FILE, {}).get(key, default)

    def set(self, key: str, value) -> None:
        with _LOCK:
            data = _read(self.FILE, {})
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
            _write(self.FILE, data)


class PluginBundles:
    """Named per-machine sets of plugins — "case types". A triage bundle
    enables lateral movement + system-info plugins, a BEC bundle the
    user-activity ones; applying a bundle sets the OPEN CASE's per-plugin
    overrides to exactly the bundle (server-side, one registry reload).
    Machine-level like the rest of workspace/: which plugins exist and
    which workflows an analyst runs are properties of the machine, not
    evidence."""

    FILE = "plugin_bundles.json"

    def list(self) -> list[dict]:
        with _LOCK:
            return _read(self.FILE, {"bundles": []})["bundles"]

    def get(self, bundle_id: int) -> dict:
        for b in self.list():
            if b["id"] == bundle_id:
                return b
        raise KeyError(f"No bundle {bundle_id}")

    def save(self, name: str, plugins: list[str]) -> dict:
        """Upsert by name — 'Triage' means one thing per machine."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Name the bundle")
        if len(name) > 100:
            raise ValueError("Bundle name is too long")
        plugins = sorted({str(p) for p in (plugins or [])})
        with _LOCK:
            data = _read(self.FILE, {"bundles": []})
            items = data["bundles"]
            existing = next((b for b in items if b["name"].lower() == name.lower()), None)
            if existing:
                existing["plugins"] = plugins
                rec = existing
            else:
                rec = {"id": _next_id(items), "name": name, "plugins": plugins, "created_at": _now()}
                items.append(rec)
            _write(self.FILE, data)
            return rec

    def delete(self, bundle_id: int) -> None:
        with _LOCK:
            data = _read(self.FILE, {"bundles": []})
            data["bundles"] = [b for b in data["bundles"] if b["id"] != bundle_id]
            _write(self.FILE, data)


filters = SavedFilters()
header_nicknames = HeaderNicknames()
timeline_templates = TimelineTemplates()
tags = TagTemplate()
column_layouts = ColumnLayouts()
import_profiles = ImportProfiles()
plugin_prefs = PluginPrefs()
plugin_bundles = PluginBundles()
machine_prefs = MachinePrefs()
app_settings = AppSettings()
