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

/* The Cmd/Ctrl+Enter shortcut, spelled for this machine. Both keys run
   the query everywhere (plugins.js, search.js); the label used to say ⌘⏎
   on every platform, which on the Windows boxes most analysts triage from
   names a key that isn't there. */
export const IS_MAC = /Mac|iPhone|iPad/.test((navigator.platform || '') + (navigator.userAgent || ''));
export const MOD_ENTER = IS_MAC ? '⌘⏎' : 'Ctrl+⏎';
/* Windows, where Chromium's prefers-reduced-motion is just the Animation
   effects switch — see splash.osSkipsSplash for why that matters. */
export const IS_WINDOWS = /^Win/.test(navigator.platform || '') || /Windows/.test(navigator.userAgent || '');

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
   removable) per browser under Settings → Grid & layout — see autofitMaxWidth.
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
/* The request never reached a server: it shut itself down (Winnow exits
   a couple of minutes after the last window closes), it crashed, or the
   machine slept through it. `fetch` reports all of that as the same bare
   TypeError — "Failed to fetch" — which every catch in the app then shows
   an analyst verbatim, so it has to be replaced with something true and
   actionable before it gets there. */
export const OFFLINE_MESSAGE =
  'lost the connection to Winnow’s server — it may have shut down';
const connListeners = [];
export function onConnectionChange(fn) { connListeners.push(fn); }
function announce(up) { for (const fn of connListeners) fn(up); }

/* Which case this page's state belongs to: the token the server named it
   by (case_token, on /api/case/current and on every route that switches
   case). Every request carries it, and the server refuses (409
   case_changed, nothing touched) one whose case isn't the one it has
   open. That is the whole of what stops a window left on case A, after
   ANOTHER window opened case B on the same server, from reading B's rows
   under A's tabs and tagging them: source ids, rids and tag ids are small
   numbers every case reuses, so nothing else on the wire tells them apart
   (server.py's case guard). Null on a page with no case open: nothing to
   check, and nothing is sent. */
let caseToken = null;
let caseSwitching = 0;
const caseListeners = [];
export function onCaseChanged(fn) { caseListeners.push(fn); }
export function setCaseToken(token) { caseToken = token || null; }
export function pageCaseToken() { return caseToken; }
export function caseHeaders() { return caseToken ? { 'X-Winnow-Case': caseToken } : {}; }

/* A download that is a plain navigation can't carry a header, so it
   carries the token in its query instead. */
export function caseUrl(path) {
  if (!caseToken) return path;
  return `${path}${path.includes('?') ? '&' : '?'}case_token=${caseToken}`;
}

/* `now` is the case the server has open ({token, name}), or null for none —
   from the presence stream (connection.js) or a refused request. Not this
   page's case means another window switched the server: say so. Ignored
   mid-switch, when it is this page's own switch landing. */
export function noticeServerCase(now) {
  if (!caseToken || caseSwitching || (now && now.token === caseToken)) return;
  for (const fn of caseListeners) fn(now);
}

/* Run a request that switches the server's case (open, save-as, discard)
   and adopt the case_token its answer names. Until it lands, a refusal of
   one of this page's own earlier requests is that switch, not another
   window's, and a request that asked about the case being left is dropped
   quietly rather than reported. */
export async function switchCase(request) {
  caseSwitching += 1;
  try {
    const res = await request();
    if (res && 'case_token' in res) setCaseToken(res.case_token);
    return res;
  } finally {
    caseSwitching -= 1;
  }
}

/* Whether a failed request was the server refusing it for naming a case it
   no longer has open. `sentCase` is the token the request went out with.
   A refusal of a request this page sent before its own switch is marked
   `aborted` (see api()): it asked about the case being left, and nobody
   needs telling. Shared with jobs.js's upload XHR. */
export function caseRefused(err, sentCase) {
  const d = err.detail;
  if (err.status !== 409 || !d || d.error !== 'case_changed') return false;
  err.caseChanged = true;
  if (caseSwitching || sentCase !== caseToken) err.aborted = true;
  else noticeServerCase(d.open || null);
  return true;
}

/* `opts.signal` (an AbortController's) rides through to fetch. A request
   this client aborted itself — a view build a newer one superseded, see
   view.js — rejects with `aborted: true` and no status, and says nothing
   about the connection: the server never answered because it was never
   allowed to, not because it is gone. */
export async function api(path, opts) {
  const o = { ...opts };
  if (o.method && o.method !== 'GET') {
    o.headers = { ...(o.headers || {}), 'X-Timeline-Lite-Client': '1' };
  }
  const sentCase = caseToken;
  if (sentCase) o.headers = { ...(o.headers || {}), 'X-Winnow-Case': sentCase };
  let r;
  try {
    r = await fetch(path, o);
  } catch (e) {
    if (e && e.name === 'AbortError') {
      const err = new Error('Cancelled');
      err.aborted = true;
      err.cause = e;
      throw err;
    }
    const err = new Error(OFFLINE_MESSAGE);
    err.offline = true;      // callers that want to say more can branch on it
    err.cause = e;
    announce(false);
    throw err;
  }
  // Any answer at all means the server is there — a 400 is the app talking.
  announce(true);
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
    caseRefused(err, sentCase);
    throw err;
  }
  return r.json();
}

export const post = (path, body, { signal } = {}) =>
  api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal });

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

/* Whether a drag carries a given type. `types` is a frozen array in
   current browsers and a DOMStringList in older WebKit — includes.call
   handles both without copying it on every dragover. */
export const dragHas = (e, type) =>
  !!(e.dataTransfer && e.dataTransfer.types && Array.prototype.includes.call(e.dataTransfer.types, type));

/* `.cancel()` drops a call still waiting — for a caller that is about
   to do the same work at once (the search box's Enter). */
export const debounce = (fn, ms) => {
  let t;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  d.cancel = () => clearTimeout(t);
  return d;
};

/* A map keyed by column name — or by any other name an analyst or a file
   supplies. Column names are user data, and a CSV can have a `toString`,
   `constructor` or `__proto__` column: a plain `{}` answers those with
   Object.prototype's members (a filter box prefilled with
   `function toString() { [native code] }`, a value picker that throws on
   `.trim()` of it), and `map['__proto__'] = x` replaces the map's
   prototype instead of adding a key — so the column can't be filtered,
   and hiding it hid every column with no layout entry of its own. A
   null-prototype object inherits nothing and keeps `__proto__` as an
   ordinary key; JSON.stringify, Object.keys/entries and `in` treat it like
   any other object. A spread (`{ ...m }`) gives back a PLAIN object, so a
   copy that is going to be looked up by name goes through here again.
   `src`'s own keys are copied in, `__proto__` included when JSON.parse
   made it one. */
export const nameMap = (src) => Object.assign(Object.create(null), src || {});
