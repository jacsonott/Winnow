/* Constants, the two DOM helpers every module uses ($ and el), the API
client, and the toast/busy chrome.

   Split out of the former single static/app.js — see CLAUDE.md. */
/* Winnow — virtualized grid over a materialised SQLite view.
   Rows are fetched in pages of PAGE and cached; only the visible window is
   ever in the DOM, so a 20M-row view scrolls the same as a 2k-row one. */

/* Fixed per-request cost (uvicorn + JSON encode) dominates at 500 — measured:
   500 rows/request is 288ms per 10k rows scrolled, 5000 rows/request is
   103ms/10k (~2.8x), 10,000 rows/request is 79ms/10k. 5000 takes most of
   that win without pushing per-request JSON payload size as far, for a
   proportionally smaller further gain. MAX_CACHED_PAGES below is sized off
   this value — keep them paired. */
export const PAGE = 5000;

export const OVERSCAN = 12;

export const ROW_H_COMFORTABLE = 24;

export const ROW_H_COMPACT = 20;

export let ROW_H = ROW_H_COMFORTABLE;
/* Rebinding has to happen in this module: an imported binding is readable
   (live — importers see the new value) but not assignable, and the old
   single-file version assigned it straight from paintDensity(). */
export function setRowH(px) { ROW_H = px; }

 // mutable — the Appearance density setting changes this at runtime, see applyDensity()

/* Ceiling on the virtualized spacer's height. The spacer (#spacerY,
   #timelineSpacerY) is what gives the scroller its scrollable height —
   row_count * ROW_H — but a DOM element can't be arbitrarily tall. Blink
   clamps at 33,554,365px (measured; 2^25 LayoutUnits) and Gecko lower still
   (~17.9M), and past the clamp the spacer stops growing while the row count
   doesn't: scrollTop stops mapping onto rows 1:1 and the tail of the view
   becomes unreachable. Measured on a 2,459,653-row $J table, which wants
   59,031,672px of spacer: scrolling bottomed out at row ~1,398,090, hiding
   43% of the evidence with no indication anything was missing.
   So the spacer is capped below every engine's limit, and above the cap every
   conversion between scrollTop and a row goes through vScroll()/rScroll()
   rather than a bare multiply or divide by ROW_H. Below the cap those are
   the identity, so nothing changes for a table that already fit.
   What the cap costs above it is scroll *granularity*, not reach: 2.46M rows
   get ~6.5px of spacer per row instead of 24, so a wheel notch travels ~3.7x
   further. Every row stays individually addressable (that needs 1px/row; the
   cap doesn't reach it until ~16M rows), and keyboard navigation moves by row
   rather than by pixel, so it's unaffected either way. */
export const MAX_SPACER_PX = 16000000;

export const GUTTER_W = 104;

 // keep in sync with `.gutter { width: ... }` in style.css
/* Default ceiling for autofit-to-content column widths, overridable (and
   removable) per browser under Settings → Appearance — see autofitMaxWidth.
   A cap exists because one pathological column can otherwise decide the
   width of the whole grid: a CommandLine full of base64 is tens of
   thousands of characters, and the rows are `width: max-content`, so an
   uncapped fit makes every horizontal scroll of every other column a
   journey. 900px is wide enough for a full Windows path, which 480 wasn't. */
export const AUTOFIT_MAX_W_DEFAULT = 900;

export const $ = (id) => document.getElementById(id);

export const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt != null) n.textContent = txt;
  return n;
};

/* ------------------------------------------------------------------ net */

/* Every non-GET call carries this header — it's what the server's CSRF
   middleware checks for (see server.py's require_client_header). A
   same-origin request always allows a custom header; a cross-origin one
   can't add it without triggering a CORS preflight, which fails since this
   app sends no CORS allow-headers. GETs are left alone since they're
   read-only and this header would break the plain-navigation download links
   (Export, session/filters export) that can't set custom headers at all. */
export async function api(path, opts) {
  const o = { ...opts };
  if (o.method && o.method !== 'GET') {
    o.headers = { ...(o.headers || {}), 'X-Timeline-Lite-Client': '1' };
  }
  const r = await fetch(path, o);
  if (!r.ok) {
    let msg = r.statusText, detail = null;
    // FastAPI's `detail` is usually a string, but a few routes raise a
    // structured one the caller needs to branch on (case-in-use, below).
    // Keep both: `message` stays the human string every existing catch
    // prints, `detail` carries the object when there is one.
    try {
      detail = (await r.json()).detail;
      if (typeof detail === 'string') msg = detail;
      else if (detail && detail.message) msg = detail.message;
    } catch {}
    // The status rides along so callers can tell "you asked for something
    // invalid" (4xx) from "the server broke" (5xx) — the server is careful
    // to only 400 the former, and blaming an analyst's filter for a backend
    // defect sends them off fixing something that isn't wrong.
    const err = new Error(msg);
    err.status = r.status;
    err.detail = detail;
    throw err;
  }
  return r.json();
}

export const post = (path, body) =>
  api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

/* Thin top-of-viewport progress indicator for anything that can take a
   real amount of time on a large source (a view rebuild — filter/sort/
   search — is the one that matters most; scroll paging is deliberately left
   out since it's normally sub-page-latency and would just flicker). A
   counter, not a boolean, so overlapping calls can't have one's `finally`
   hide the bar while another is still in flight. */
export let busyCount = 0;

export function setBusy(on) {
  busyCount = Math.max(0, busyCount + (on ? 1 : -1));
  $('busyBar').hidden = busyCount === 0;
}

export function toast(msg, ms = 2600) {
  const t = $('toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

/* A toast with one thing you can do about it. Longer-lived than a plain
   toast (it's only useful if it's still there when you look up) and
   dismissed by acting on it. */
export function toastAction(msg, label, onclick, ms = 12000) {
  const t = $('toast');
  t.replaceChildren(el('span', null, msg));
  const btn = el('button', 'toast-action', label);
  btn.onclick = () => { t.hidden = true; onclick(); };
  t.append(btn);
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

export const debounce = (fn, ms) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};
