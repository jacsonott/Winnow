"""Reading Plaso storage files (.plaso) for import — the flat-timeline
sibling of xlsxread.py, stdlib only (sqlite3/json/zlib), so the airgap
rule holds.

A .plaso file is a SQLite database of serialized "attribute containers"
written by log2timeline. Two on-disk generations matter, and this module
reads both by LOOKING at the file rather than trusting a version number:

- **Serialized events** (storage format 20170707, plaso ≤ ~2021): the
  `event` table is `(_identifier, _timestamp, _data)` where `_data` is a
  JSON document (zlib-compressed to a BLOB when the metadata table says
  `compression_format: zlib`) holding `timestamp`, `timestamp_desc` and an
  `_event_data_row_identifier` pointing into `event_data`.

- **Schema-column events** (the acstore era, plaso ≥ ~2022): the `event`
  table has one real column per schema attribute — `timestamp` (BIGINT,
  POSIX microseconds), `timestamp_desc` (TEXT), `date_time` (JSON TEXT)
  and `_event_data_identifier` (TEXT, ``"event_data.<row>"``).

Either way `event_data` stays a serialized JSON blob per row — its
attributes vary per data_type, which is exactly why it can't be columns —
and that variability drives the output shape here: a handful of fixed,
always-present columns (Datetime / Timestamp desc / Data type / Parser /
Source file / Host / User) plus ONE `Attributes (JSON)` column holding
everything else as a JSON object. Winnow already knows how to lift fields
out of a JSON cell into real columns (structparse's derived-column ops),
so the long tail stays reachable without a 400-column sparse table.

Events stream out ordered by timestamp, so rid order in the ingested
table IS chronological order — the unfiltered view reads as a timeline
with no sort applied (and stays on invariant #2's root_virtual fast
path). Values that plaso's JSON serializer encodes as tagged dicts
(``{"__type__": "bytes", ...}``, path specs) are flattened to readable
text; a malformed row lands as an error note in its Attributes cell
rather than aborting a multi-million-event import.
"""

from __future__ import annotations

import base64
import datetime
import json
import sqlite3
import zlib
from typing import Any, Iterator

PLASO_COLUMNS = [
    "Datetime", "Timestamp desc", "Data type", "Parser",
    "Source file", "Host", "User", "Attributes (JSON)",
]
PLASO_COLUMN_TYPES = ["datetime", "text", "text", "text", "text", "text", "text", "text"]

# event_data attributes lifted into fixed columns (everything else goes to
# the JSON cell). display_name/filename feed "Source file"; parser is the
# plaso parser chain that produced the event.
_LIFTED = {"data_type", "parser", "display_name", "filename", "hostname", "username"}

# How many decoded event_data rows to keep around: events sharing one
# event_data row (MACB expansion) arrive adjacent in timestamp order, so a
# small cache absorbs nearly every repeat without holding a million-entry
# dict on a big timeline.
_DATA_CACHE = 4096


def _connect(path: str) -> sqlite3.Connection:
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    ro.row_factory = sqlite3.Row
    return ro


def _metadata(ro: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in ro.execute("SELECT key, value FROM metadata")}


def _table_columns(ro: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in ro.execute(f"PRAGMA table_info({table})")]


def is_plaso_storage(path: str) -> bool:
    """True when the file is a SQLite db with plaso's metadata + event
    tables — cheap enough to be a validity check before a long import."""
    try:
        ro = _connect(path)
    except sqlite3.Error:
        return False
    try:
        names = {r["name"] for r in ro.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"metadata", "event", "event_data"} <= names:
            return False
        return "format_version" in _metadata(ro)
    except sqlite3.Error:
        return False
    finally:
        ro.close()


def plaso_summary(path: str) -> dict:
    """Event count + format facts, for validation errors and tests."""
    ro = _connect(path)
    try:
        meta = _metadata(ro)
        count = ro.execute("SELECT COUNT(*) FROM event").fetchone()[0]
        return {
            "event_count": count,
            "format_version": meta.get("format_version", ""),
            "compression_format": meta.get("compression_format", ""),
            "serialization_format": meta.get("serialization_format", "json"),
        }
    finally:
        ro.close()


def _loads(blob: Any, compression: str) -> dict:
    if isinstance(blob, (bytes, bytearray)):
        if compression == "zlib":
            blob = zlib.decompress(bytes(blob))
        blob = blob.decode("utf-8", "replace")
    doc = json.loads(blob)
    if not isinstance(doc, dict):
        raise ValueError("serialized container is not an object")
    return doc


def _plain(value: Any) -> Any:
    """A JSON-serializer-tagged value -> something readable in a cell.
    plaso encodes bytes as {'__type__': 'bytes', 'stream': base64} and
    path specs as nested {'__type__': 'PathSpec', ...}; neither belongs in
    an Attributes cell verbatim."""
    if isinstance(value, dict):
        t = value.get("__type__")
        if t == "bytes":
            try:
                return f"<{len(base64.b64decode(value.get('stream', '')))} bytes>"
            except (ValueError, TypeError):
                return "<bytes>"
        if t == "PathSpec":
            loc = _spec_location(value)
            return loc if loc else {k: _plain(v) for k, v in value.items() if not k.startswith("__")}
        return {k: _plain(v) for k, v in value.items() if not k.startswith("__")}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _spec_location(spec: Any) -> str:
    """The outermost location in a path-spec chain — the path the analyst
    means when they ask 'which file did this come from'."""
    while isinstance(spec, dict):
        loc = spec.get("location")
        if isinstance(loc, str) and loc:
            return loc
        spec = spec.get("parent")
    return ""


def _ts_iso(ts: Any) -> str:
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if ts == 0:
        return ""
    try:
        dt = datetime.datetime(1970, 1, 1) + datetime.timedelta(microseconds=ts)
    except OverflowError:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def iter_plaso_rows(path: str) -> Iterator[list[str]]:
    """Yields one PLASO_COLUMNS-aligned row per event, ordered by
    timestamp. Cancellation lives in the caller's batch loop
    (Store.ingest_plaso), which owns the drop-the-partial contract."""
    ro = _connect(path)
    try:
        meta = _metadata(ro)
        if meta.get("serialization_format", "json") != "json":
            raise ValueError(f"Unsupported plaso serialization format: {meta.get('serialization_format')}")
        compression = meta.get("compression_format", "")
        event_cols = set(_table_columns(ro, "event"))
        serialized_events = "_data" in event_cols
        order_col = "_timestamp" if "_timestamp" in event_cols else "timestamp"

        data_cache: dict[int, dict] = {}

        def event_data_for(ident: int | None) -> dict:
            if ident is None:
                return {}
            hit = data_cache.get(ident)
            if hit is not None:
                return hit
            row = ro.execute("SELECT _data FROM event_data WHERE _identifier=?", (ident,)).fetchone()
            try:
                doc = _loads(row["_data"], compression) if row else {}
            except (ValueError, zlib.error) as e:
                doc = {"_winnow_error": f"unreadable event_data row {ident}: {e}"}
            if len(data_cache) >= _DATA_CACHE:
                data_cache.clear()
            data_cache[ident] = doc
            return doc

        stream_locations: dict[int, str] = {}
        have_streams = bool(ro.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='event_data_stream'").fetchone())

        def stream_location(ident: int | None) -> str:
            if ident is None or not have_streams:
                return ""
            if ident not in stream_locations:
                row = ro.execute(
                    "SELECT _data FROM event_data_stream WHERE _identifier=?", (ident,)).fetchone()
                loc = ""
                if row:
                    try:
                        doc = _loads(row["_data"], compression)
                        loc = _spec_location(doc.get("path_spec"))
                    except (ValueError, zlib.error):
                        loc = ""
                stream_locations[ident] = loc
            return stream_locations[ident]

        def ref_int(value: Any) -> int | None:
            # "_event_data_row_identifier": 12  (serialized era)
            # "_event_data_identifier": "event_data.12"  (schema era)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and "." in value:
                tail = value.rsplit(".", 1)[1]
                if tail.isdigit():
                    return int(tail)
            return None

        if serialized_events:
            cursor = ro.execute(f"SELECT _data FROM event ORDER BY {order_col}")
        else:
            sel = [c for c in ("timestamp", "timestamp_desc", "_event_data_identifier") if c in event_cols]
            cursor = ro.execute(f"SELECT {', '.join(sel)} FROM event ORDER BY {order_col}")

        while True:
            rows = cursor.fetchmany(2000)
            if not rows:
                break
            for r in rows:
                if serialized_events:
                    try:
                        ev = _loads(r["_data"], compression)
                    except (ValueError, zlib.error):
                        continue
                    timestamp = ev.get("timestamp")
                    ts_desc = ev.get("timestamp_desc", "")
                    data_ref = ref_int(ev.get("_event_data_row_identifier",
                                              ev.get("_event_data_identifier")))
                else:
                    ev = {}
                    timestamp = r["timestamp"] if "timestamp" in r.keys() else None
                    ts_desc = r["timestamp_desc"] if "timestamp_desc" in r.keys() else ""
                    data_ref = ref_int(r["_event_data_identifier"]
                                       if "_event_data_identifier" in r.keys() else None)

                data = event_data_for(data_ref)
                stream_ref = ref_int(data.get("_event_data_stream_identifier",
                                              data.get("_event_data_stream_row_identifier")))
                # A very old storage inlines event_data attributes on the
                # event itself — fold them in so nothing is dropped.
                merged = {**{k: v for k, v in ev.items()
                             if not k.startswith("_") and not k.startswith("__")
                             and k not in ("timestamp", "timestamp_desc")},
                          **{k: v for k, v in data.items()
                             if not k.startswith("_") and not k.startswith("__")}}

                extras = {k: _plain(v) for k, v in sorted(merged.items()) if k not in _LIFTED}
                if "_winnow_error" in data:
                    extras["_winnow_error"] = data["_winnow_error"]
                source_file = _cell(merged.get("display_name") or "") \
                    or stream_location(stream_ref) or _cell(merged.get("filename") or "")
                yield [
                    _ts_iso(timestamp),
                    _cell(ts_desc),
                    _cell(merged.get("data_type")),
                    _cell(merged.get("parser")),
                    source_file,
                    _cell(merged.get("hostname")),
                    _cell(merged.get("username")),
                    json.dumps(extras, ensure_ascii=False, sort_keys=True) if extras else "",
                ]
    finally:
        ro.close()
