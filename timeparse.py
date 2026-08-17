"""Timestamp-parsing operations for derived datetime columns.

Each operation turns one raw cell value (arbitrary analyst data — epoch
integers, BSD syslog lines, ISO variants, FILETIME ticks, ...) into the
canonical shape the rest of the app already understands:

    "YYYY-MM-DD HH:MM:SS"            second-resolution sources
    "YYYY-MM-DD HH:MM:SS.ffffff"     sub-second sources (fixed 6 digits)

That shape is deliberate: store.py's _TS_ISO_RE / TS_NORMALIZE / DAY_BUCKET
and app.js's parseTimestamp all prefix-match it today, so a derived column's
values sort, filter, group and display with zero new regexes to keep in sync
(the two hand-synced regex twins in store.py/app.js stay untouched). The
fraction is fixed-width so lexicographic order == chronological order, and
sub-second precision is kept when the source has it — a timestomped value
with .000000 subseconds next to populated ones is a classic tell (same
rationale as examples/plugins/mft_usn's filetime_to_iso).

Timezone rules (per the feature decision): values carrying an explicit
offset (ISO Z/±HH:MM, Apache CLF, RFC 2822) are converted to UTC; epoch
family values are inherently UTC; naive text values keep their components
exactly as written — no TZ database, no DST guessing — unless the analyst
sets the optional `utc_offset` param, a *fixed* offset the value is shifted
by to reach UTC. Never `datetime.fromtimestamp` without an explicit tz, and
never locale-dependent %b month names (airgapped analysis boxes are often
not en_US) — months go through the explicit MONTHS table.

Unparseable input returns None (stored as NULL — excluded from timeframe
filters and comparisons rather than masquerading as data). Empty input is
None too; the caller distinguishes "empty" from "failed to parse" when
counting failures.

register_op() is module-internal for now but is the single seam a future
PluginAPI.register_timestamp_op would call — don't inline the registry.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable

# Explicit month table instead of strptime's %b (locale-dependent).
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Same plausibility window store.py's _webkit_to_iso uses: a numeric value
# only counts as a timestamp in some unit if the resulting date is one an
# analyst could plausibly be looking at. This is also what makes epoch
# auto-ranging deterministic — the s/ms/us/ns windows are x1000 apart while
# the window itself spans only ~x6.5, so at most one unit ever fits.
PLAUSIBLE_MIN_YEAR = 1990
PLAUSIBLE_MAX_YEAR = 2100

_UTC = timezone.utc
_FILETIME_EPOCH = datetime(1601, 1, 1)
_DOTNET_EPOCH = datetime(1, 1, 1)
_MAC_EPOCH = datetime(2001, 1, 1, tzinfo=_UTC)
# Lotus 1-2-3 convention (day 1 = 1900-01-01, with the phantom 1900-02-29
# Excel inherited) — 1899-12-30 as day zero makes modern serials come out
# right without special-casing the 1900 leap bug for dates that predate
# any log an analyst will meet.
_EXCEL_EPOCH = datetime(1899, 12, 30)

_OFFSET_RE = re.compile(r"^([+-])(\d{1,2}):?(\d{2})?$")


def _parse_utc_offset(text: str) -> timedelta:
    """"+05:30" / "-0700" / "+5" / "Z"/"UTC" -> a fixed timedelta. Raises
    ValueError on anything else — a bad offset should fail column creation
    loudly, not silently produce unshifted values."""
    s = str(text).strip()
    if s.upper() in ("Z", "UTC", "+00:00", "-00:00", ""):
        return timedelta(0)
    m = _OFFSET_RE.match(s)
    if not m:
        raise ValueError(f"Bad UTC offset {text!r} — expected e.g. +05:30 or -0700")
    sign = -1 if m.group(1) == "-" else 1
    hours, minutes = int(m.group(2)), int(m.group(3) or 0)
    if hours > 14 or minutes > 59:
        raise ValueError(f"Bad UTC offset {text!r} — beyond ±14:00")
    return sign * timedelta(hours=hours, minutes=minutes)


def _fmt(dt: datetime, subsecond: bool) -> str:
    """Canonical output. An aware datetime is converted to UTC first; a naive
    one is emitted exactly as its components stand (the no-TZ-math rule)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(_UTC).replace(tzinfo=None)
    if subsecond:
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _plausible(dt: datetime) -> bool:
    return PLAUSIBLE_MIN_YEAR <= dt.year <= PLAUSIBLE_MAX_YEAR


def _shift_naive(dt: datetime, params: dict) -> datetime:
    """Apply the optional fixed `utc_offset` param to a naive local-time
    value: UTC = local - offset (a -05:00 log line moves 5 hours later)."""
    off = params.get("utc_offset")
    if off:
        return dt - _parse_utc_offset(off)
    return dt


OPERATIONS: "dict[str, dict[str, Any]]" = {}


def register_op(op: dict) -> None:
    """Insertion order is the autodetect tie-break priority — register the
    less ambiguous / more common interpretation first."""
    if "detect" not in op:
        op["detect"] = _success_rate_detect(op)
    op.setdefault("two_input", False)
    op.setdefault("stateful", False)
    op.setdefault("value_type", "datetime")
    op.setdefault("hidden_from_detect", False)
    op.setdefault("params", [])
    OPERATIONS[op["id"]] = op


def _success_rate_detect(op: dict) -> Callable:
    def detect(samples: Iterable, params: dict) -> float:
        vals = [v for v in samples if v is not None and str(v).strip()]
        if not vals:
            return 0.0
        state: dict = {}
        ok = 0
        for v in vals:
            if op["parse"](v, params, state) is not None:
                ok += 1
        return ok / len(vals)
    return detect


def detect_defaults(op: dict) -> dict:
    """Params to run detection with, before the analyst has filled any in —
    required params fall back to their detect_default (e.g. syslog's base
    year := the current UTC year, a provisional value good enough to judge
    'does this look like syslog at all')."""
    out = {}
    for spec in op["params"]:
        if spec.get("default") is not None:
            out[spec["name"]] = spec["default"]
        elif spec.get("detect_default") == "current_year":
            out[spec["name"]] = datetime.now(_UTC).year
    return out


def detect(samples: Iterable) -> list[dict]:
    """Rank every visible operation by the fraction of non-empty samples it
    parses. Stable sort ⇒ registry order breaks confidence ties (the same
    sample-and-round-trip spirit as store.py's preview_sqlite_tables
    heuristic for WebKit columns). Genuinely ambiguous inputs (a number in
    both the unix-seconds and Mac-absolute windows) legitimately return two
    high-confidence rows — the caller's live preview is the disambiguator."""
    results = []
    for op in OPERATIONS.values():
        if op["hidden_from_detect"]:
            continue
        params = detect_defaults(op)
        conf = op["detect"](samples, params)
        if conf > 0:
            results.append({"op_id": op["id"], "label": op["label"], "confidence": conf, "params": params})
    results.sort(key=lambda r: -r["confidence"])
    return results


def validate_params(op_id: str, params: dict | None) -> dict:
    """Normalize and validate analyst-supplied params against the op's
    schema. Returns only known keys, coerced; raises ValueError with a
    message fit to show the analyst."""
    op = OPERATIONS.get(op_id)
    if op is None:
        raise ValueError(f"Unknown operation: {op_id}")
    params = params or {}
    out: dict = {}
    for spec in op["params"]:
        name = spec["name"]
        val = params.get(name)
        if val is None or (isinstance(val, str) and not val.strip()):
            if spec.get("required"):
                raise ValueError(f"Operation {op['label']!r} needs the {spec.get('label', name)!r} parameter")
            if spec.get("default") is not None:
                out[name] = spec["default"]
            continue
        kind = spec.get("type", "text")
        if kind == "int":
            try:
                val = int(val)
            except (TypeError, ValueError):
                raise ValueError(f"{spec.get('label', name)} must be a whole number")
            lo, hi = spec.get("min"), spec.get("max")
            if (lo is not None and val < lo) or (hi is not None and val > hi):
                raise ValueError(f"{spec.get('label', name)} must be between {lo} and {hi}")
        elif kind == "select":
            val = str(val)
            if val not in spec["options"]:
                raise ValueError(f"{spec.get('label', name)} must be one of: {', '.join(spec['options'])}")
        elif kind == "offset":
            val = str(val).strip()
            _parse_utc_offset(val)  # raises on garbage
        else:
            val = str(val).strip()
        out[name] = val
    return out


def list_ops() -> list[dict]:
    """JSON-able registry listing for the UI — everything but the callables."""
    out = []
    for op in OPERATIONS.values():
        out.append({
            "id": op["id"],
            "label": op["label"],
            "description": op["description"],
            "params": op["params"],
            "two_input": op["two_input"],
            "value_type": op["value_type"],
            "derived_kind": op.get("derived_kind", "datetime"),
        })
    return out


_UTC_OFFSET_PARAM = {
    "name": "utc_offset", "label": "Source UTC offset", "type": "offset",
    "required": False,
    "help": "If these timestamps are local time, the fixed offset to shift them to UTC (e.g. -05:00). Leave blank to keep them as written.",
}


# ----------------------------------------------------------- epoch family

_EPOCH_UNITS = (("s", 1), ("ms", 1_000), ("us", 1_000_000), ("ns", 1_000_000_000))


def _parse_unix(value: Any, params: dict, state: dict) -> str | None:
    s = str(value).strip()
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    unit = params.get("unit", "auto")
    for name, scale in _EPOCH_UNITS:
        if unit != "auto" and unit != name:
            continue
        try:
            dt = datetime.fromtimestamp(v / scale, tz=_UTC)
        except (OverflowError, OSError, ValueError):
            continue
        if not _plausible(dt):
            continue
        # Sub-second output when the resolved unit is finer than seconds, or
        # a seconds value carries an explicit fraction.
        return _fmt(dt, subsecond=(name != "s" or "." in s))
    return None


register_op({
    "id": "unix_epoch",
    "label": "Unix epoch",
    "description": "Seconds/milliseconds/microseconds/nanoseconds since 1970-01-01 UTC. Auto mode picks the one unit that lands in a plausible year (1990–2100).",
    "params": [{
        "name": "unit", "label": "Unit", "type": "select",
        "options": ["auto", "s", "ms", "us", "ns"], "default": "auto",
    }],
    "subsecond": True,
    "parse": _parse_unix,
})


# FILETIMEs past year 9999 overflow datetime — seen in the wild in
# timestomped/corrupt records (same guard as examples/plugins/mft_usn).
_FILETIME_MAX = int((datetime(9999, 12, 28) - _FILETIME_EPOCH).total_seconds() * 10_000_000)
_BARE_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")


def _parse_filetime(value: Any, params: dict, state: dict) -> str | None:
    s = str(value).strip()
    ticks: int | None = None
    if s.lower().startswith("0x"):
        try:
            ticks = int(s, 16)
        except ValueError:
            return None
    elif s.isdigit():
        ticks = int(s)
    elif _BARE_HEX_RE.match(s) and re.search(r"[A-Fa-f]", s):
        # Bare hex only when a letter proves it's hex — an all-digit string
        # is a plausible decimal timestamp and stays decimal.
        ticks = int(s, 16)
    if ticks is None or ticks <= 0 or ticks > _FILETIME_MAX:
        return None
    dt = _FILETIME_EPOCH + timedelta(microseconds=ticks // 10)
    if not _plausible(dt):
        return None
    return _fmt(dt, subsecond=True)


register_op({
    "id": "windows_filetime",
    "label": "Windows FILETIME",
    "description": "100-nanosecond ticks since 1601-01-01 UTC ($MFT, registry, AD attributes). Accepts decimal, 0x-prefixed hex, or bare hex containing a letter.",
    "subsecond": True,
    "parse": _parse_filetime,
})


# Same constant as store.py's WEBKIT_EPOCH_OFFSET_US; kept local so this
# module stays importable on its own (and store.py's SQLite-ingest path
# keeps its own conversion untouched).
_WEBKIT_OFFSET_US = 11_644_473_600_000_000


def _parse_webkit(value: Any, params: dict, state: dict) -> str | None:
    s = str(value).strip()
    try:
        v = int(float(s))
    except (TypeError, ValueError):
        return None
    unix_us = v - _WEBKIT_OFFSET_US
    if unix_us <= 0:
        return None
    try:
        dt = datetime.fromtimestamp(unix_us / 1_000_000, tz=_UTC)
    except (OverflowError, OSError, ValueError):
        return None
    if not _plausible(dt):
        return None
    return _fmt(dt, subsecond=True)


register_op({
    "id": "webkit_epoch",
    "label": "WebKit/Chrome epoch",
    "description": "Microseconds since 1601-01-01 UTC — Chromium History/Cookies/Downloads timestamps.",
    "subsecond": True,
    "parse": _parse_webkit,
})


# ----------------------------------------------------------- text family

_SYSLOG_RE = re.compile(r"^([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})")


def _parse_syslog(value: Any, params: dict, state: dict) -> str | None:
    m = _SYSLOG_RE.match(str(value).strip())
    if not m:
        return None
    month = MONTHS.get(m.group(1).lower())
    if month is None:
        return None
    # Year rollover: syslog lines carry no year, so the analyst supplies the
    # year of the FIRST line and we advance it whenever the month decreases
    # while walking the file in rid order (Dec 31 → Jan 1; a multi-year file
    # increments once per wrap). Assumption, documented in the op
    # description: syslog files are appended chronologically — an
    # out-of-order line whose month is lower than its predecessor's gets
    # attributed to the next year. (A refinement — only increment when the
    # drop exceeds ~6 months — would tolerate small reorderings, but the
    # simple rule is predictable and matches what log2timeline does.)
    if not state:
        state["year"] = int(params["base_year"])
        state["last_month"] = None
    if state["last_month"] is not None and month < state["last_month"]:
        state["year"] += 1
    state["last_month"] = month
    try:
        dt = datetime(state["year"], month, int(m.group(2)),
                      int(m.group(3)), int(m.group(4)), int(m.group(5)))
    except ValueError:
        # e.g. Feb 29 against a non-leap resolved year — genuinely ambiguous
        # evidence, surfaced as a parse failure rather than guessed at.
        return None
    return _fmt(_shift_naive(dt, params), subsecond=False)


register_op({
    "id": "syslog_bsd",
    "label": "BSD syslog (Mmm dd hh:mm:ss)",
    "description": "Classic syslog timestamps with no year. Set the year of the first line; the year advances automatically when the file crosses Dec 31 → Jan 1 (assumes lines are appended chronologically).",
    "params": [
        {"name": "base_year", "label": "Year of first line", "type": "int",
         "required": True, "min": 1970, "max": 2100, "detect_default": "current_year"},
        _UTC_OFFSET_PARAM,
    ],
    "subsecond": False,
    "stateful": True,
    "parse": _parse_syslog,
})


_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})"
    r"(?::(\d{2})(?:[.,](\d{1,9}))?)?"
    r"\s*(Z|[+-]\d{2}:?\d{2})?"
)


def _parse_iso(value: Any, params: dict, state: dict) -> str | None:
    m = _ISO_RE.match(str(value).strip())
    if not m:
        return None
    y, mo, d, h, mi, sec, frac, off = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0),
                      int(frac[:6].ljust(6, "0")) if frac else 0)
    except ValueError:
        return None
    if off:
        # Explicit offset → convert to UTC. "+0530" and "+05:30" both allowed.
        dt = dt.replace(tzinfo=timezone(_parse_utc_offset(off)))
    else:
        dt = _shift_naive(dt, params)
    return _fmt(dt, subsecond=bool(frac))


register_op({
    "id": "iso8601",
    "label": "ISO 8601",
    "description": "YYYY-MM-DDThh:mm:ss with optional fraction (. or ,) and offset. Z/±HH:MM values are converted to UTC; values without an offset are kept as written.",
    "params": [_UTC_OFFSET_PARAM],
    "subsecond": True,
    "parse": _parse_iso,
})


_DD_MMM_RE = re.compile(r"^(\d{1,2})[ -]([A-Za-z]{3})[ -](\d{4})[ ,]+(\d{1,2}):(\d{2}):(\d{2})")


def _parse_dd_mmm(value: Any, params: dict, state: dict) -> str | None:
    m = _DD_MMM_RE.match(str(value).strip())
    if not m:
        return None
    month = MONTHS.get(m.group(2).lower())
    if month is None:
        return None
    try:
        dt = datetime(int(m.group(3)), month, int(m.group(1)),
                      int(m.group(4)), int(m.group(5)), int(m.group(6)))
    except ValueError:
        return None
    return _fmt(_shift_naive(dt, params), subsecond=False)


register_op({
    "id": "dd_mmm_yyyy",
    "label": "dd Mmm yyyy hh:mm:ss",
    "description": "e.g. \"05 Jan 2024 13:22:01\" (also 5-Jan-2024). English month abbreviations.",
    "params": [_UTC_OFFSET_PARAM],
    "subsecond": False,
    "parse": _parse_dd_mmm,
})


_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$")


def _parse_compact(value: Any, params: dict, state: dict) -> str | None:
    m = _COMPACT_RE.match(str(value).strip())
    if not m:
        return None
    try:
        dt = datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None
    if not _plausible(dt):
        return None
    return _fmt(_shift_naive(dt, params), subsecond=False)


register_op({
    "id": "compact_ymd",
    "label": "Compact YYYYMMDDhhmmss",
    "description": "14-digit run-together timestamps (e.g. 20240105132201), common in tool exports and filenames.",
    "params": [_UTC_OFFSET_PARAM],
    "subsecond": False,
    "parse": _parse_compact,
})


def _parse_mac(value: Any, params: dict, state: dict) -> str | None:
    s = str(value).strip()
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    try:
        dt = _MAC_EPOCH + timedelta(seconds=v)
    except OverflowError:
        return None
    if not _plausible(dt):
        return None
    return _fmt(dt, subsecond="." in s)


register_op({
    "id": "mac_absolute",
    "label": "Mac absolute (Cocoa) time",
    "description": "Seconds since 2001-01-01 UTC (macOS/iOS plists, sqlite stores). Overlaps the unix-seconds range — check the preview to pick the right one.",
    "subsecond": True,
    "parse": _parse_mac,
})


def _parse_excel(value: Any, params: dict, state: dict) -> str | None:
    s = str(value).strip()
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    # Round to the nearest second: a fractional day stored as a float
    # carries ~µs-scale binary noise that would otherwise surface as
    # spurious .999999 subseconds.
    try:
        dt = _EXCEL_EPOCH + timedelta(seconds=round(v * 86400))
    except OverflowError:
        return None
    if not _plausible(dt):
        return None
    return _fmt(_shift_naive(dt, params), subsecond=False)


register_op({
    "id": "excel_serial",
    "label": "Excel serial date",
    "description": "Days since 1899-12-30, fraction of a day = time of day (spreadsheet exports).",
    "params": [_UTC_OFFSET_PARAM],
    "subsecond": False,
    "parse": _parse_excel,
})


def _parse_dotnet(value: Any, params: dict, state: dict) -> str | None:
    s = str(value).strip()
    if not s.isdigit():
        return None
    ticks = int(s)
    dt_max_ticks = 3155378975999999999  # DateTime.MaxValue.Ticks
    if ticks <= 0 or ticks > dt_max_ticks:
        return None
    dt = _DOTNET_EPOCH + timedelta(microseconds=ticks // 10)
    if not _plausible(dt):
        return None
    return _fmt(dt, subsecond=True)


register_op({
    "id": "dotnet_ticks",
    "label": ".NET ticks",
    "description": "100-nanosecond ticks since 0001-01-01 (DateTime.Ticks, some log frameworks). Overlaps the nanosecond-epoch range — check the preview.",
    "subsecond": True,
    "parse": _parse_dotnet,
})


_CLF_RE = re.compile(r"^\[?(\d{1,2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})\s*([+-]\d{4})?")


def _parse_clf(value: Any, params: dict, state: dict) -> str | None:
    m = _CLF_RE.match(str(value).strip())
    if not m:
        return None
    month = MONTHS.get(m.group(2).lower())
    if month is None:
        return None
    try:
        dt = datetime(int(m.group(3)), month, int(m.group(1)),
                      int(m.group(4)), int(m.group(5)), int(m.group(6)))
    except ValueError:
        return None
    if m.group(7):
        dt = dt.replace(tzinfo=timezone(_parse_utc_offset(m.group(7))))
    else:
        dt = _shift_naive(dt, params)
    return _fmt(dt, subsecond=False)


register_op({
    "id": "apache_clf",
    "label": "Apache access log",
    "description": "dd/Mmm/yyyy:hh:mm:ss +zzzz (Common Log Format, with or without the surrounding brackets). Offset-bearing values are converted to UTC.",
    "params": [_UTC_OFFSET_PARAM],
    "subsecond": False,
    "parse": _parse_clf,
})


def _parse_rfc2822(value: Any, params: dict, state: dict) -> str | None:
    try:
        dt = parsedate_to_datetime(str(value).strip())
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None or not _plausible(dt):
        return None
    if dt.tzinfo is None:
        dt = _shift_naive(dt, params)
    return _fmt(dt, subsecond=False)


register_op({
    "id": "rfc2822",
    "label": "RFC 2822 (email date)",
    "description": "Mon, 02 Jan 2006 15:04:05 -0700 (email headers, HTTP dates). Offset-bearing values are converted to UTC.",
    "params": [_UTC_OFFSET_PARAM],
    "subsecond": False,
    "parse": _parse_rfc2822,
})


_US_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})[ ,]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM|am|pm)?")


def _parse_us(value: Any, params: dict, state: dict) -> str | None:
    m = _US_RE.match(str(value).strip())
    if not m:
        return None
    mo, d, y, h, mi, sec, ampm = m.groups()
    hh = int(h)
    if ampm:
        # Same 12-hour rules as store.py's _ts_normalize US branch.
        if ampm.lower() == "pm" and hh < 12:
            hh += 12
        if ampm.lower() == "am" and hh == 12:
            hh = 0
    try:
        dt = datetime(int(y), int(mo), int(d), hh, int(mi), int(sec or 0))
    except ValueError:
        return None
    return _fmt(_shift_naive(dt, params), subsecond=False)


register_op({
    "id": "us_datetime",
    "label": "US MM/DD/YYYY hh:mm:ss",
    "description": "US-style dates with optional AM/PM (Timeline Explorer / EZTools exports).",
    "params": [_UTC_OFFSET_PARAM],
    "subsecond": False,
    "parse": _parse_us,
})


# ----------------------------------------------------------- duration delta

_LENIENT_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?)?"
)


def parse_datetime_lenient(value: Any) -> datetime | None:
    """A naive datetime from either shape the app already treats as a
    datetime — the canonical/ISO one (which every derived datetime column
    holds) or the US one — for computing deltas. Offsets are deliberately
    not handled here: delta inputs should be datetime-typed columns, which
    are canonical or were typed at ingest by the same two regexes."""
    if value is None:
        return None
    s = str(value).strip()
    m = _LENIENT_ISO_RE.match(s)
    if m:
        y, mo, d, h, mi, sec, frac = m.groups()
        try:
            return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(sec or 0),
                            int((frac or "")[:6].ljust(6, "0")) if frac else 0)
        except ValueError:
            return None
    m = _US_RE.match(s)
    if m:
        mo, d, y, h, mi, sec, ampm = m.groups()
        hh = int(h)
        if ampm:
            if ampm.lower() == "pm" and hh < 12:
                hh += 12
            if ampm.lower() == "am" and hh == 12:
                hh = 0
        try:
            return datetime(int(y), int(mo), int(d), hh, int(mi), int(sec or 0))
        except ValueError:
            return None
    return None


def _parse_delta_pair(end_raw: Any, start_raw: Any, params: dict) -> str | None:
    end = parse_datetime_lenient(end_raw)
    start = parse_datetime_lenient(start_raw)
    if end is None or start is None:
        return None
    return f"{(end - start).total_seconds():.6f}"


register_op({
    "id": "duration_delta",
    "label": "Duration between two columns",
    "description": "Time difference (this column minus another datetime column), stored as seconds. Negative when this column is earlier.",
    "params": [{
        "name": "other_column", "label": "Subtract column", "type": "column", "required": True,
        "help": "The start time; the derived value is this column's time minus that column's.",
    }],
    "subsecond": True,
    "two_input": True,
    "value_type": "number",
    "derived_kind": "duration",
    "hidden_from_detect": True,
    "parse": None,
    "parse_pair": _parse_delta_pair,
})
