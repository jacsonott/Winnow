/* Timestamp parsing and the layered display-format resolution.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { S } from './state.js';

/* --------------------------------------------------- timestamp display format */

/* Presentation only — the stored/exported value is always the raw text the
   CSV came with (invariant: source data is never mutated). Deliberately
   NOT `new Date(string)`: that applies the browser's local timezone to
   whatever gets parsed, which can silently shift an evidentiary timestamp.
   Instead this pulls numeric components straight out of the same two
   families store.py's DATE_RE already recognizes as "datetime" at ingest,
   then formatting is pure string padding — no Date object, no TZ math. A
   value that doesn't match either shape is left exactly as it was. */
export const TS_ISO_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?)?/;

export const TS_US_RE = /^(\d{1,2})\/(\d{1,2})\/(\d{4})(?:[ ,]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM|am|pm)?)?/;

/* Month-name family ("JUN 23 2026 00:11:00", "23 Jun 2026", "June 23,
   2026 5:11 PM") — the word is captured loose and validated against
   MONTH_NO below, mirroring store.py's third recognized shape. */
export const TS_MONTH_RE = /^(?:([A-Za-z]{3,9}) +(\d{1,2})|(\d{1,2}) +([A-Za-z]{3,9})) *,? *(\d{4})(?:[ ,]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM|am|pm)?)?/;

const MONTH_NO = {
  jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6,
  jul: 7, aug: 8, sep: 9, sept: 9, oct: 10, nov: 11, dec: 12,
  january: 1, february: 2, march: 3, april: 4, june: 6,
  july: 7, august: 8, september: 9, october: 10, november: 11, december: 12,
};

export function parseTimestamp(raw) {
  const s = String(raw).trim();
  let m = TS_ISO_RE.exec(s);
  if (m) {
    return {
      y: +m[1], mo: +m[2], d: +m[3], h: +(m[4] || 0), mi: +(m[5] || 0), s: +(m[6] || 0),
      /* Sub-second digits kept as the raw string, padded/truncated only at
         format time — parsing them to a number would lose the distinction
         between .1 (100ms) and .000001, which matters for ordering
         same-second events. */
      frac: m[7] || '',
    };
  }
  m = TS_US_RE.exec(s);
  if (m) {
    let h = +(m[4] || 0);
    const ampm = (m[7] || '').toLowerCase();
    if (ampm === 'pm' && h < 12) h += 12;
    if (ampm === 'am' && h === 12) h = 0;
    return { y: +m[3], mo: +m[1], d: +m[2], h, mi: +(m[5] || 0), s: +(m[6] || 0), frac: '' };
  }
  m = TS_MONTH_RE.exec(s);
  if (m) {
    const mo = MONTH_NO[(m[1] || m[4]).toLowerCase()];
    if (!mo) return null; // a weekday or plain word, not a month
    let h = +(m[6] || 0);
    const ampm = (m[9] || '').toLowerCase();
    if (ampm === 'pm' && h < 12) h += 12;
    if (ampm === 'am' && h === 12) h = 0;
    return { y: +m[5], mo, d: +(m[2] || m[3]), h, mi: +(m[7] || 0), s: +(m[8] || 0), frac: '' };
  }
  return null;
}

export const pad2 = (n) => String(n).padStart(2, '0');

export const TS_FORMATS = {
  raw: 'As stored',
  iso: 'YYYY-MM-DD HH:MM:SS',
  iso_ms: 'YYYY-MM-DD HH:MM:SS.mmm',
  iso_us: 'YYYY-MM-DD HH:MM:SS.ffffff',
  date: 'YYYY-MM-DD',
  time: 'HH:MM:SS',
  us: 'MM/DD/YYYY HH:MM:SS',
  us_date: 'MM/DD/YYYY',
};

/* Sub-second digits to exactly `n` places. A value with no fraction shows
   zeros rather than being left short, so the column stays column-aligned
   and two timestamps remain visually comparable. */
export function fracTo(frac, n) { return (frac || '').padEnd(n, '0').slice(0, n); }

export function formatTimestamp(raw, fmt) {
  if (!fmt || fmt === 'raw' || raw == null || raw === '') return raw;
  const t = parseTimestamp(raw);
  if (!t) return raw; // doesn't match a recognized shape — show unchanged, never fabricate
  const ymd = `${t.y}-${pad2(t.mo)}-${pad2(t.d)}`;
  const hms = `${pad2(t.h)}:${pad2(t.mi)}:${pad2(t.s)}`;
  switch (fmt) {
    case 'iso': return `${ymd} ${hms}`;
    case 'iso_ms': return `${ymd} ${hms}.${fracTo(t.frac, 3)}`;
    case 'iso_us': return `${ymd} ${hms}.${fracTo(t.frac, 6)}`;
    case 'date': return ymd;
    case 'time': return hms;
    case 'us': return `${pad2(t.mo)}/${pad2(t.d)}/${t.y} ${hms}`;
    case 'us_date': return `${pad2(t.mo)}/${pad2(t.d)}/${t.y}`;
    default: return raw;
  }
}

/* Four layers, most specific first: this column in this table, then this
   case, then the system-wide default, then 'iso'. Before derived columns
   existed the fallback was 'raw' — analysts who prefer that set it as
   their system default (Settings > Timestamps); an explicitly-chosen
   per-column format still wins, so existing saved layouts are unaffected. */
export function tsFormatFor(name) {
  return (S.layout[name] || {}).tsFormat
    || S.caseSettings.ts_format
    || S.appSettings.default_ts_format
    || 'iso';
}

/* A duration column holds seconds (see timeparse's duration_delta). Shown
   humanized by default because "1h 23m 45s" is the question the analyst
   asked; the raw seconds stay one click away in the header menu. */
export function formatDuration(raw, mode) {
  if (raw == null || raw === '' || mode === 'raw') return raw;
  const total = Number(raw);
  if (!isFinite(total)) return raw;
  const sign = total < 0 ? '-' : '';
  let rest = Math.abs(total);
  const h = Math.floor(rest / 3600); rest -= h * 3600;
  const m = Math.floor(rest / 60); rest -= m * 60;
  const s = Math.round(rest * 1000) / 1000;
  const parts = [];
  if (h) parts.push(h + 'h');
  if (m || h) parts.push(m + 'm');
  parts.push(s + 's');
  return sign + parts.join(' ');
}

export function columnMeta(name) { return S.columns.find((c) => c.name === name) || null; }

/* The imported file's own columns. Header-set identity (saved filters,
   cross-case layouts, nicknames, timeline templates) has to key off these
   alone — a derived column is one analyst's addition, and including it
   would stop the same file matching its own saved work elsewhere. */
export function baseColumns() { return S.columns.filter((c) => !c.derived); }

/* Presentation for one cell, given its column's type. Kept in one place
   because four call sites (grid, grouped grid, detail pane, copy) have to
   agree on what the analyst is looking at. */
export function displayCell(name, val) {
  const c = columnMeta(name);
  if (!c) return val;
  if (c.derived_kind === 'duration') return formatDuration(val, (S.layout[name] || {}).durFormat);
  if (c.type === 'datetime') return formatTimestamp(val, tsFormatFor(name));
  return val;
}
