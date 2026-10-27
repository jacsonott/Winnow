/* Lateral-movement tab (v3) — the reference example for a plugin-shipped
   UI. Everything is drawn from Winnow's own CSS tokens, read at draw time,
   so a theme switch is a repaint, and the case timeframe filter applies
   here as it does to any grid.

   One feed (the plugin's `feed` route) holds every hop the ticked
   movement events draw: hosts folded to one name however each export
   spells them, the account kept on every hop, to the minute. Five views
   read it:

   - Chain over time: a lane per host, an arrow per link where it first
     appears, credential switches as diamonds, sessions as bars;
   - Host matrix: sources down the side, destinations across the top;
   - By account: accounts ranked by what is unusual about them;
   - Pivot (a host's right-click): where its access came from, and where
     it went next;
   - Path (a host's right-click): time-ordered routes between two hosts,
     each step where the account changes explained or not.

   Under them, the hops per bin across the case: drag across it to zoom
   every view into a window. Above them, the movement-event panel (which
   events, read from which tables), the builder and the Events… manager.
   Nothing is built on request: ticking an event reads the feed again. */

let S = null;     // definitions, ticks, fetch bookkeeping; rebuilt with the mount
let ctx = null;   // the winnow context
let D = null;     // the tab's own DOM nodes
let st = null;    // what the views show: mode, window, filters, marks
let F = null;     // the current computation (compute()), for hover redraws
let hit = null;   // what the view on screen can hover or right-click

const STATE_VERSION = 1;
const OPEN_TAIL = 60;              // an open-ended session is drawn as an hour, fading
const SWITCH_LOOKBACK = 12 * 60;   // how long an arrival stays on a host, for a switch
const SWITCH_TRIES = 20;           // departures tried per (host, account) before giving up
const DWELL = 12 * 60;             // the longest wait between two hops of a route
const ROUTE_BUDGET = 2e6;          // route relaxations before a path search stops
const LINEUP = 1;                  // minutes either side for a hop with no account to back one up
const MATRIX_ROWS = 300;           // sources the matrix lists, busiest first
const ACCOUNT_ROWS = 400;          // accounts By account lists, most unusual first
const PIVOT_CAP = 16;              // hosts per pivot column
const MODES = [['chain', 'Chain over time'], ['matrix', 'Host matrix'], ['accounts', 'By account'],
  ['pivot', 'Pivot'], ['path', 'Path']];
// Colour slots (--series-N): the events the views read for meaning keep
// the same hue whatever else is ticked; the rest take the others in turn.
const PLAIN_SLOTS = [1, 2, 5, 6, 4];

// Hosts and accounts are numbered once per mount, so a pivot, a crown
// jewel or a filter survives the feed being read again.
let HOSTS, hostIx, USERS, userIx, MACHINE, NOACCT;
function resetNames() {
  HOSTS = []; hostIx = new Map(); USERS = []; userIx = new Map(); MACHINE = [];
  NOACCT = userId('');   // a hop from an event with no user column
}
function hostId(n) {
  if (!hostIx.has(n)) { hostIx.set(n, HOSTS.length); HOSTS.push(n); }
  return hostIx.get(n);
}
function userId(n) {
  if (!userIx.has(n)) { userIx.set(n, USERS.length); USERS.push(n); MACHINE.push(n.endsWith('$')); }
  return userIx.get(n);
}

// The feed, unpacked onto those numbers. k is the event type (one colour,
// one chip), sel the selection (one event on one table) the hop came from.
let SELS = [], EV = [], HOPS = [], EXPL = [], SESS = [], PER = [], SPELL = new Map(), USPELL = new Map();
let IPMAP = {}, TRUNC = false, T0M = 0, SPAN = 1, PRIORITY = [], SUCCEEDED = new Set(), ON = new Set();
let FEED_GEN = 0;   // bumped with every feed, for what is cached from one

let PAL = {};
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function readPalette() {
  PAL = { ink: css('--ink'), panel: css('--panel'), panel2: css('--panel-2'), line: css('--line'),
    line2: css('--line-2'), text: css('--text'), dim: css('--dim'), faint: css('--text-faint') || css('--dim'),
    accent: css('--accent'), accentFg: css('--accent-fg'), danger: css('--danger'),
    mono: css('--mono') || 'monospace', ui: css('--ui') || 'sans-serif', series: [] };
  for (let i = 1; i <= 8; i++) PAL.series[i] = css(`--series-${i}`) || PAL.accent;
}
const evColor = (k) => PAL.series[EV[k] ? EV[k].slot : 1];
function rgba(color, a) {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color || '');
  if (!m) return color;
  const h = m[1].length === 3 ? m[1].replace(/./g, (c) => c + c) : m[1];
  const n = parseInt(h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad2 = (n) => String(n).padStart(2, '0');
// Minutes are relative to the feed's first one; the time columns are read
// as UTC, which is how they read in the grid.
function fmt(m, day = true) {
  const d = new Date((T0M + m) * 60000);
  return (day ? `${MON[d.getUTCMonth()]} ${d.getUTCDate()} ` : '') + `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
}
const fmtDay = (m) => fmt(m).slice(0, -6);
const dayOf = (m) => Math.floor((T0M + m) / 1440);
const caseDays = () => dayOf(SPAN - 1) - dayOf(0) + 1;
const plural = (n, w) => `${n.toLocaleString()} ${w}${n === 1 ? '' : 's'}`;
const dur = (m) => (m < 60 ? `${m} min` : m < 2880 ? `${Math.floor(m / 60)} h ${pad2(m % 60)} min` : `${Math.round(m / 1440)} days`);
function splitAcct(u) { const i = u.indexOf('\\'); return i < 0 ? ['', u] : [u.slice(0, i + 1), u.slice(i + 1)]; }
const acctName = (u) => (u === NOACCT ? '(no account)' : USERS[u]);
const shortAcct = (u) => (u === NOACCT ? '(no account)' : splitAcct(USERS[u])[1]);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const shortName = (name) => String(name).replace(/\s*\([^()]*\)$/, '') || name;
const listOf = (v) => (Array.isArray(v) ? v : String(v ?? '').split(',')).map((x) => String(x).trim()).filter(Boolean);
function lowerBound(arr, t) {
  let lo = 0, hi = arr.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid].t < t) lo = mid + 1; else hi = mid; }
  return lo;
}

/* ================================================================== mount */

export default function mount(container, winnow) {
  ctx = winnow;
  const { el } = winnow;
  const was = st;
  S = { defs: { shipped: [], saved: [], header_sets: {} }, selections: [], seq: 0, timer: 0, saveOn: false,
    busy: false, error: null, note: null, pendingNote: null, tfKey: '', started: false };
  st = { mode: 'chain', off: new Set(), hideMachine: true, newOnly: false, account: null, host: null, win: null,
    pivot: null, path: null, pathFrom: null, routeHi: null, hoverAcct: null, hoverHost: null,
    expected: new Set(), jewels: new Set(), panelOpen: was ? was.panelOpen : false, useTf: true };
  resetNames();
  applyFeed(null, []);
  F = null; hit = null;

  const link = el('link'); link.rel = 'stylesheet';
  link.href = `${winnow.assets}/ui/tab.css`;
  container.append(link);
  const root = el('div', 'lm-root');
  container.append(root);
  D = { root };

  // ---- the bar: which view, the case timeframe, what is on screen
  const bar = el('div', 'lm-bar');
  D.modes = el('div', 'lm-seg');
  D.modes.setAttribute('role', 'tablist');
  D.modes.setAttribute('aria-label', 'View');
  const tf = el('label', 'lm-toggle');
  D.useTf = el('input'); D.useTf.type = 'checkbox'; D.useTf.checked = st.useTf;
  D.useTf.onchange = () => { st.useTf = D.useTf.checked; saveState(); scheduleFetch(0); };
  tf.title = 'Read only the hops inside the timeframe filter set on the tables tab';
  tf.append(D.useTf, el('span', null, 'Apply case timeframe'));
  D.status = el('span', 'lm-status');
  bar.append(D.modes, tf, D.status);

  // ---- the movement events: which events, on which tables
  const head = el('div', 'lm-panel-head');
  D.caret = el('button', 'lm-caret');
  D.caret.type = 'button';
  D.caret.title = 'Show or hide the event list';
  D.caret.onclick = () => { st.panelOpen = !st.panelOpen; S.note = null; paintHead(); saveState(); };
  D.summary = el('span', 'lm-panel-summary');
  const manage = el('button', 'btn ghost', 'Events…');
  manage.title = 'Add, edit and copy movement-event definitions';
  manage.onclick = () => openEventManager(winnow);
  const newBtn = el('button', 'btn ghost', '+ New event type');
  newBtn.title = 'Define a movement event mapped to your own columns';
  newBtn.onclick = () => openEventBuilder(winnow, { onSave: addEvent });
  head.append(D.caret, el('span', 'lm-panel-title', 'Movement events'), D.summary, manage, newBtn);
  D.panel = el('div', 'lm-events');

  // ---- filters over the views
  D.filters = el('div', 'lm-filters');
  D.kinds = el('span', 'lm-kinds');
  const hm = el('label', 'lm-toggle');
  D.hideMachine = el('input'); D.hideMachine.type = 'checkbox'; D.hideMachine.checked = st.hideMachine;
  D.hideMachine.onchange = () => { st.hideMachine = D.hideMachine.checked; saveState(); render(); };
  hm.append(D.hideMachine, el('span', null, 'Hide machine accounts ($)'));
  D.newOnlyWrap = el('label', 'lm-toggle');
  D.newOnly = el('input'); D.newOnly.type = 'checkbox';
  D.newOnly.onchange = () => { st.newOnly = D.newOnly.checked; render(); };
  D.newOnlyWrap.append(D.newOnly, el('span', null, 'Only links new in this window'));
  D.chips = el('span', 'lm-chips');
  D.legend = el('span', 'lm-legend');
  D.filters.append(D.kinds, el('span', 'lm-fsep'), hm, D.newOnlyWrap, D.chips, D.legend);

  // ---- the views, and the hops over time under them
  D.wrap = el('div', 'lm-stage-wrap');
  D.stage = el('div', 'lm-stage');
  D.tip = el('div', 'lm-tip'); D.tip.hidden = true;
  D.wrap.append(D.stage, D.tip);
  D.hist = el('div', 'lm-hist');
  const histTop = el('div', 'lm-hist-top');
  D.histLabel = el('span', 'lm-hist-label');
  D.whole = el('button', 'btn ghost lm-small', 'Whole case');
  D.whole.type = 'button';
  D.whole.onclick = () => { st.win = null; st.newOnly = false; st.routeHi = null; render(); };
  histTop.append(D.histLabel, D.whole);
  D.histCanvas = el('canvas');
  D.histCanvas.setAttribute('aria-label', 'Hops over time; drag across to set a window');
  D.hist.append(histTop, D.histCanvas);

  root.append(bar, head, D.panel, D.filters, D.wrap, D.hist);
  wireStage();
  wireHist();

  // A canvas is drawn at a size, and the tab's size changes with the
  // window and the panel above it; the app's theme is attributes on <html>.
  const ro = new ResizeObserver(() => { if (root.isConnected) schedule(); else ro.disconnect(); });
  ro.observe(D.stage);
  const mo = new MutationObserver(() => { if (root.isConnected) schedule(); else mo.disconnect(); });
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-style', 'style'] });

  paintHead();
  render();
  start();
}

/* The saved state first, then the feed: nothing is written until the
   case's saved ticks have been read back, or the first paint would save
   an empty panel over them. A read that failed is not "nothing saved",
   so this mount then never saves at all. */
async function start() {
  await loadDefs();
  let saved = null;
  if (ctx.tabState) {
    saved = await ctx.tabState.get();
    if (saved && saved.error) { saved = null; S.saveOn = false; } else S.saveOn = true;
  }
  const p = saved && saved.payload;
  if (p && p.v === STATE_VERSION) restore(p);
  else tickEverythingThatBinds();
  S.started = true;
  // closed is the usual state, the summary says what is on; open when
  // there is nothing on, since that is where to start
  if (!resolvedSelections().length) st.panelOpen = true;
  S.tfKey = JSON.stringify(timeframe());
  renderPanel(); paintHead(); render();
  scheduleFetch(0);
}

// A case opened for the first time draws what the shipped events find in
// it, rather than an empty tab waiting for ticks.
function tickEverythingThatBinds() {
  for (const src of liveSources()) {
    for (const d of eventsFor(src)) S.selections.push({ key: selKey(src.id, d.name), source_id: src.id, name: d.name });
  }
}

// What was saved records what somebody wanted, not what the case still
// holds: a table can be gone, or an event no longer bind to it.
function restore(p) {
  const sources = new Map(liveSources().map((s) => [s.id, s]));
  const byName = new Map(allDefs().map((d) => [d.name, d]));
  for (const [id, name] of Array.isArray(p.sel) ? p.sel : []) {
    const src = sources.get(id), d = byName.get(name);
    if (src && d && bindsTo(d, src) && !S.selections.some((s) => s.key === selKey(id, name))) {
      S.selections.push({ key: selKey(id, name), source_id: id, name });
    }
  }
  if (['chain', 'matrix', 'accounts'].includes(p.mode)) st.mode = p.mode;
  st.off = new Set((p.off || []).map(String));
  st.hideMachine = p.hideMachine !== false;
  st.panelOpen = p.panelOpen === true;
  st.useTf = p.useTf !== false;
  st.jewels = new Set((p.jewels || []).map((h) => hostId(String(h))));
  st.expected = new Set((p.expected || []).map((u) => userId(String(u))));
  D.hideMachine.checked = st.hideMachine;
  D.useTf.checked = st.useTf;
}

function saveState() {
  if (!S || !S.saveOn || !S.started || !ctx.tabState) return;
  ctx.tabState.set({
    v: STATE_VERSION,
    sel: S.selections.map((s) => [s.source_id, s.name]),
    mode: ['chain', 'matrix', 'accounts'].includes(st.mode) ? st.mode : 'chain',
    off: [...st.off].slice(0, 100),
    hideMachine: st.hideMachine,
    panelOpen: st.panelOpen,
    useTf: st.useTf,
    jewels: [...st.jewels].map((h) => HOSTS[h]).slice(0, 200),
    expected: [...st.expected].map((u) => USERS[u]).slice(0, 200),
  });
}

export function onShow() {
  // Tables, definitions and the timeframe may all have changed while
  // another tab was showing.
  if (!S || !ctx || !S.started) return;
  loadDefs().then(() => {
    const before = S.selections.map((s) => s.key).join();
    syncSelections();
    renderPanel(); paintHead();
    const tfKey = JSON.stringify(timeframe());
    if (S.selections.map((s) => s.key).join() !== before || (st.useTf && tfKey !== S.tfKey)) {
      saveState();
      scheduleFetch(0);
    } else render();
  });
}

/* ================================================================== feed */

const liveSources = () => ctx.state.sources.filter((s) => !s.error);
const allDefs = () => [...S.defs.shipped, ...S.defs.saved];
const selKey = (sourceId, name) => `${sourceId}::${name}`;
const eventsFor = (src) => allDefs().filter((d) => bindsTo(d, src));

function timeframe() {
  const tr = ctx.state.timeRange;
  return st.useTf && tr && tr.enabled && (tr.start || tr.end) ? { start: tr.start || '', end: tr.end || '' } : {};
}

// The ticks, with their definitions and tables, in panel order.
function resolvedSelections() {
  const byName = new Map(allDefs().map((d) => [d.name, d]));
  const sources = new Map(liveSources().map((s) => [s.id, s]));
  const out = [];
  for (const s of S.selections) {
    const def = byName.get(s.name), src = sources.get(s.source_id);
    if (def && src) out.push({ key: s.key, source_id: s.source_id, def, src });
  }
  return out;
}

// Busy from the moment a read is due, not from when it starts: what a
// save adds is reported once the feed holds it, and a repaint in between
// would otherwise report it from the feed before.
function scheduleFetch(delay = 200) {
  clearTimeout(S.timer);
  S.busy = true;
  S.timer = setTimeout(fetchFeed, delay);
}

async function fetchFeed() {
  const my = ++S.seq;
  const sels = resolvedSelections();
  const time = timeframe();
  S.tfKey = JSON.stringify(time);
  if (!sels.length) {
    S.busy = false; S.error = null;
    applyFeed(null, []);
    render();
    return;
  }
  S.busy = true;
  paintStatus();
  try {
    const r = await ctx.post(`${ctx.base}/feed`, {
      selections: sels.map(({ source_id, def }) => ({
        source_id, name: def.name, src_col: def.src_col, dst_col: def.dst_col,
        label_col: def.label_col || null, actor_col: def.actor_col || null, time_col: def.time_col || null,
        strip_prefix: !!def.strip_prefix, conditions: def.conditions || [], session: def.session || null,
      })),
      time,
    });
    if (my !== S.seq || !D.root.isConnected) return;
    S.busy = false; S.error = null;
    applyFeed(r, sels, time);
  } catch (e) {
    if (my !== S.seq) return;
    S.busy = false; S.error = e.message;
  }
  renderPanel();
  render();
}

function applyFeed(r, sels, time = {}) {
  SELS = sels;
  FEED_GEN++;
  if (!r) {
    EV = []; HOPS = []; EXPL = []; SESS = []; PER = []; SPELL = new Map(); USPELL = new Map();
    IPMAP = {}; TRUNC = false; SPAN = 1; S.timeApplied = false;
    derive();
    return;
  }
  S.timeApplied = !!(time.start || time.end);
  const prevT0 = T0M;
  EV = [];
  const kOf = new Map();
  const selK = sels.map((s, i) => {
    if (!kOf.has(s.def.name)) {
      kOf.set(s.def.name, EV.length);
      EV.push({ k: EV.length, name: s.def.name, def: s.def, short: s.def.short || shortName(s.def.name),
        fails: !!s.def.fails, explicit: !!s.def.actor_col, session: !!s.def.session,
        saved: S.defs.saved.some((d) => d.name === s.def.name), sels: [] });
    }
    const k = kOf.get(s.def.name);
    EV[k].sels.push(i);
    return k;
  });
  let plain = 0;
  for (const e of EV) e.slot = e.fails ? 8 : e.explicit ? 7 : e.session ? 3 : PLAIN_SLOTS[plain++ % PLAIN_SLOTS.length];
  const H = r.hosts.map(hostId), U = r.users.map(userId);
  T0M = Math.round(r.t0 / 60);
  SPAN = Math.max(1, r.span);
  // a window stays on the same minutes when the feed starts somewhere else
  if (st.win && prevT0) {
    const shift = prevT0 - T0M;
    const w = [Math.max(0, st.win[0] + shift), Math.min(SPAN, st.win[1] + shift)];
    st.win = w[1] - w[0] >= 1 ? w : null;
  }
  HOPS = r.hops.map(([sel, s, d, u, t, n]) => ({ k: selK[sel], sel, s: H[s], d: H[d], u: U[u], t, n, also: null }));
  EXPL = r.explicit.map(([sel, s, d, a, u, t]) => ({ k: selK[sel], sel, s: H[s], d: H[d], a: U[a], u: U[u], t }));
  SESS = r.sessions.map(([sel, s, d, u, a, b]) => ({ k: selK[sel], sel, s: H[s], d: H[d], u: U[u], a, b }));
  SPELL = new Map(Object.entries(r.spellings).map(([h, list]) => [H[+h], list.map(([sel, role, raw, n]) => ({ sel, role, raw, n }))]));
  USPELL = new Map(Object.entries(r.account_spellings).map(([u, list]) => [U[+u], list.map(([sel, raw, n]) => ({ sel, raw, n }))]));
  PER = r.per;
  IPMAP = r.ipmap || {};
  TRUNC = !!r.truncated;
  derive();
}

// A link is coloured by its most telling event: one that names the acting
// account, then failed attempts, then sessions, then the rest in order.
function derive() {
  const rank = (e) => (e.explicit ? 0 : e.fails ? 1 : e.session ? 2 : 3);
  PRIORITY = [...EV].sort((a, b) => rank(a) - rank(b) || a.k - b.k).map((e) => e.k);
  SUCCEEDED = new Set(EV.filter((e) => !e.fails).map((e) => e.k));
}
const telling = (kinds) => { const k = PRIORITY.find((x) => kinds[x] > 0); return k == null ? 0 : k; };

/* =============================================================== compute */

const linkKey = (h) => `${h.s}|${h.d}|${h.u}`;
const shown = (u) => !(st.hideMachine && MACHINE[u]) && !st.expected.has(u);

/* A hop with no account (a flow) that lines up with a ticked hop that has
   one, on the same two hosts within a minute, is the same movement seen
   twice: it backs that hop up (h.also) instead of drawing a link of its
   own, and hides with it when its account does. Only what no log with an
   account recorded draws by itself. */
function compute() {
  const B = computeBase();
  const [a, b] = st.win || [0, SPAN];
  const inWin = B.base.filter((h) => h.t >= a && h.t <= b && (!st.newOnly || !st.win || B.first.get(linkKey(h)) >= a));
  const mine = (u) => st.account == null || u === st.account;
  const hops = inWin.filter((h) => mine(h.u));
  const sw = B.sw.filter((x) => x.t >= a && x.t <= b);
  const sess = SESS.filter((x) => ON.has(x.k) && shown(x.u) && mine(x.u)
    && (st.host == null || x.s === st.host || x.d === st.host) && x.a <= b && (x.b ?? x.a + OPEN_TAIL) >= a);
  return { base: B.base, first: B.first, inWin, hops, a, b, sw, swShown: sw.filter((x) => mine(x.to) || mine(x.from)), sess, shown: {} };
}

// What does not depend on the window or the account: the hops each view
// starts from, when each link first appears, and the credential switches.
// Kept until the feed or one of its inputs changes, so a drag across the
// timeline or a switch of view does not work them out again.
let baseCache = null;
function computeBase() {
  const key = [FEED_GEN, [...ON].join(), st.host, st.hideMachine, [...st.expected].sort().join()].join('|');
  if (baseCache && baseCache.key === key) return baseCache;
  const ticked = HOPS.filter((h) => ON.has(h.k) && (st.host == null || h.s === st.host || h.d === st.host));
  const named = new Map();
  for (const h of ticked) {
    h.also = null;
    if (h.u !== NOACCT) named.set(`${h.s}|${h.d}|${h.t}`, h);
  }
  const lone = (h) => {
    if (h.u !== NOACCT) return true;
    let tw = null;
    for (let dt = 0; dt <= LINEUP && !tw; dt++) tw = named.get(`${h.s}|${h.d}|${h.t - dt}`) || named.get(`${h.s}|${h.d}|${h.t + dt}`);
    if (!tw) return true;
    if (!tw.also) tw.also = new Map();
    tw.also.set(h.k, (tw.also.get(h.k) || 0) + h.n);
    return false;
  };
  const base = ticked.filter((h) => lone(h) && shown(h.u));
  const first = new Map();
  for (const h of base) { const k = linkKey(h); if (!first.has(k) || h.t < first.get(k)) first.set(k, h.t); }
  baseCache = { key, base, first, sw: findSwitches(base) };
  return baseCache;
}
// Everything compute() reads: a render with the same key reuses the last
// computation (and what each view cached on it).
const computeKey = () => [FEED_GEN, [...ON].join(), st.host, st.hideMachine, [...st.expected].sort().join(),
  st.win ? st.win.join('-') : '', st.newOnly, st.account].join('|');

/* A credential switch, after Hopper (Ho et al., USENIX Security 2021): an
   account leaves a host that a DIFFERENT account had just arrived on, the
   leaving account never logged on there itself, and it leaves that host on
   no other day (so a desk's own user, or a nightly push, is habit and not
   a switch). An event with an acting account (a 4648) states the same
   thing outright: who did it, and whose credentials it used. Hops with no
   account cannot say who arrived, and failed attempts arrive nowhere, so
   neither takes part. */
function findSwitches(base) {
  const hops = base.filter((h) => SUCCEEDED.has(h.k) && !MACHINE[h.u] && h.u !== NOACCT);
  const leftDays = new Map(), arrivals = new Map();
  for (const h of hops) {   // in time order, so each host's arrivals are too
    const k = `${h.s}|${h.u}`;
    if (!leftDays.has(k)) leftDays.set(k, new Set());
    leftDays.get(k).add(dayOf(h.t));
    if (!arrivals.has(h.d)) arrivals.set(h.d, []);
    arrivals.get(h.d).push(h);
  }
  const sessOn = new Map();
  for (const x of SESS) {
    if (!ON.has(x.k) || !shown(x.u) || x.u === NOACCT) continue;
    if (!sessOn.has(x.d)) sessOn.set(x.d, []);
    sessOn.get(x.d).push(x);
  }
  const explBy = new Map();
  for (const e of EXPL) {
    if (!ON.has(e.k) || !shown(e.a)) continue;
    const k = `${e.s}|${e.u}`;
    if (!explBy.has(k)) explBy.set(k, []);
    explBy.get(k).push(e);
  }
  const onAt = (host, t) => {
    const arr = arrivals.get(host) || [], out = [];
    for (let i = lowerBound(arr, t - SWITCH_LOOKBACK); i < arr.length && arr[i].t <= t; i++) {
      out.push({ u: arr[i].u, t: arr[i].t, via: arr[i].s });
    }
    for (const x of sessOn.get(host) || []) {
      if (x.a <= t && (x.b ?? x.a + OPEN_TAIL) >= t) out.push({ u: x.u, t: x.a, via: x.s, session: x });
    }
    return out.sort((p, q) => q.t - p.t);
  };
  const out = new Map(), tries = new Map();
  for (const h of hops) {
    const key = `${h.s}|${h.u}`;
    if (out.has(key)) continue;
    const ex = (explBy.get(key) || []).find((e) => Math.abs(e.t - h.t) <= 1);
    if (ex) {
      const came = onAt(h.s, ex.t).find((c) => c.u === ex.a);
      out.set(key, { how: 'explicit', host: h.s, from: ex.a, to: h.u, t: ex.t, dst: ex.d,
        fromAt: came ? came.t : null, fromVia: came ? came.via : null, session: came && came.session, also: [] });
      continue;
    }
    if (EV[h.k].explicit) continue;                 // its own acting account said nothing different
    if (leftDays.get(key).size > 1) continue;       // leaves from here on other days: habit
    const n = (tries.get(key) || 0) + 1;
    if (n > SWITCH_TRIES) continue;
    tries.set(key, n);
    const here = onAt(h.s, h.t);
    if (here.some((c) => c.u === h.u)) continue;    // it came in on its own credentials
    const others = here.filter((c) => c.u !== h.u);
    if (!others.length) continue;
    const c = others[0];
    out.set(key, { how: 'inferred', host: h.s, from: c.u, to: h.u, t: h.t, dst: h.d, fromAt: c.t, fromVia: c.via,
      session: c.session, also: [...new Set(others.slice(1).map((o) => o.u))].filter((u) => u !== c.u) });
  }
  return [...out.values()];
}

/* Routes from one host to another, after BloodHound's pathfinding but over
   time: each hop leaves no earlier than the one before it arrived, and
   within DWELL of it. A step where the account changes is explained when a
   credential switch accounts for it, a gap when nothing does, and unknown
   when one side carries no account at all (a flow). Routes with fewer gaps
   rank first, then fewer unknowns, then fewer hops, then the earlier
   arrival; without that, every desk's morning logon to a domain controller
   is a two-hop route to anything the controller ever touched. */
function findRoutes(F, from, targets) {
  const hs = F.inWin.filter((h) => SUCCEEDED.has(h.k) && h.s !== h.d);
  const outOf = new Map();
  hs.forEach((h, i) => { if (!outOf.has(h.s)) outOf.set(h.s, []); outOf.get(h.s).push(i); });
  const swAt = new Map(F.sw.map((x) => [`${x.host}|${x.from}|${x.to}`, x]));
  const step = (p, q) => (p.u === NOACCT || q.u === NOACCT ? 'unknown' : q.u === p.u ? null : swAt.get(`${p.d}|${p.u}|${q.u}`) || 'gap');
  const less = (a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2] || a[3] - b[3];
  const best = new Array(hs.length).fill(null), prev = new Array(hs.length).fill(-1);
  const heap = [];
  const push = (i) => {
    heap.push(i);
    for (let c = heap.length - 1; c > 0;) {
      const p = (c - 1) >> 1;
      if (less(best[heap[c]], best[heap[p]]) >= 0) break;
      [heap[c], heap[p]] = [heap[p], heap[c]]; c = p;
    }
  };
  const pop = () => {
    const top = heap[0], last = heap.pop();
    if (heap.length) {
      heap[0] = last;
      for (let p = 0; ;) {
        const l = 2 * p + 1, r = l + 1;
        let m = p;
        if (l < heap.length && less(best[heap[l]], best[heap[m]]) < 0) m = l;
        if (r < heap.length && less(best[heap[r]], best[heap[m]]) < 0) m = r;
        if (m === p) break;
        [heap[m], heap[p]] = [heap[p], heap[m]]; p = m;
      }
    }
    return top;
  };
  hs.forEach((h, i) => { if (h.s === from) { best[i] = [0, 0, 1, h.t]; push(i); } });
  const done = new Uint8Array(hs.length);
  let budget = ROUTE_BUDGET;
  while (heap.length && budget > 0) {
    const i = pop();
    if (done[i]) continue;
    done[i] = 1;
    const p = hs[i];
    if (targets.has(p.d)) continue;
    const outs = outOf.get(p.d) || [];
    // each host's hops are in time order: start at the first that leaves
    // no earlier than this one arrived
    let lo = 0, hi = outs.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (hs[outs[mid]].t < p.t) lo = mid + 1; else hi = mid; }
    for (let x = lo; x < outs.length; x++) {
      const j = outs[x], q = hs[j];
      if (q.t - p.t > DWELL) break;
      if (--budget <= 0) break;
      if (q.d === from) continue;
      const s = step(p, q);
      const cand = [best[i][0] + (s === 'gap' ? 1 : 0), best[i][1] + (s === 'unknown' ? 1 : 0), best[i][2] + 1, q.t];
      if (!best[j] || less(cand, best[j]) < 0) { best[j] = cand; prev[j] = i; push(j); }
    }
  }
  const seen = new Map();
  hs.forEach((h, j) => {
    if (!best[j] || !targets.has(h.d)) return;
    const hops = [];
    for (let k = j; k >= 0; k = prev[k]) hops.unshift(hs[k]);
    const hosts = [from, ...hops.map((x) => x.d)];
    if (new Set(hosts).size !== hosts.length) return;   // went round in a circle
    const key = hosts.join('>');
    if (seen.has(key) && less(seen.get(key).score, best[j]) <= 0) return;
    seen.set(key, { hops, hosts, score: best[j], steps: hops.slice(1).map((q, n) => step(hops[n], q)) });
  });
  return { routes: [...seen.values()].sort((a, b) => less(a.score, b.score)).slice(0, 4), cut: budget <= 0 };
}

function aggregate(hops, keyFn, first) {
  const m = new Map();
  for (const h of hops) {
    const key = keyFn(h);
    let L = m.get(key);
    if (!L) {
      L = { s: h.s, d: h.d, u: h.u, n: 0, first: h.t, last: h.t, kinds: EV.map(() => 0), also: EV.map(() => 0),
        users: new Map(), sels: new Map(), fresh: false };
      m.set(key, L);
    }
    L.n += h.n; L.kinds[h.k] += h.n;
    if (h.also) for (const [k, n] of h.also) L.also[k] += n;
    if (h.t < L.first) L.first = h.t;
    if (h.t > L.last) L.last = h.t;
    L.users.set(h.u, (L.users.get(h.u) || 0) + h.n);
    L.sels.set(h.sel, (L.sels.get(h.sel) || 0) + h.n);
    if (st.win && first.get(linkKey(h)) >= st.win[0]) L.fresh = true;
  }
  return [...m.values()];
}

/* ================================================================ chrome */

const pathLabel = (p) => `${HOSTS[p.from]} → ${p.jewels ? 'crown jewels' : HOSTS[p.to]}`;
function paintModes() {
  const { el } = ctx;
  D.modes.replaceChildren();
  for (const [id, label] of MODES) {
    if (id === 'pivot' && st.pivot == null) continue;
    if (id === 'path' && st.path == null) continue;
    const b = el('button');
    b.type = 'button';
    b.setAttribute('role', 'tab');
    b.dataset.mode = id;
    b.setAttribute('aria-selected', String(st.mode === id));
    if (id === 'pivot' || id === 'path') {
      b.append(el('span', null, id === 'pivot' ? `Pivot: ${HOSTS[st.pivot]}` : `Path: ${pathLabel(st.path)}`));
      const x = el('span', 'lm-x', '✕');
      x.title = id === 'pivot' ? 'Close the pivot' : 'Close the path';
      x.onclick = (e) => { e.stopPropagation(); st[id] = null; if (st.mode === id) st.mode = 'chain'; render(); };
      b.append(x);
    } else b.textContent = label;
    b.onclick = () => { st.mode = id; saveState(); render(); };
    D.modes.append(b);
  }
}

function paintHead() {
  D.caret.textContent = st.panelOpen ? '▾' : '▸';
  D.caret.setAttribute('aria-expanded', String(st.panelOpen));
  D.panel.hidden = !st.panelOpen;
  const offered = liveSources().reduce((n, src) => n + eventsFor(src).length, 0);
  const on = resolvedSelections().length;
  D.summary.replaceChildren(document.createTextNode(offered ? `${on} of ${offered} on` : 'none offered here'));
  if (S.note) { D.summary.append(' · '); D.summary.append(ctx.el('b', null, S.note)); }
}

function paintFilters() {
  const { el } = ctx;
  D.filters.hidden = !EV.length;
  D.kinds.replaceChildren();
  for (const e of EV) {
    const b = el('button', 'lm-kind');
    b.type = 'button';
    b.setAttribute('aria-pressed', String(!st.off.has(e.name)));
    b.title = `${e.name}: click to ${st.off.has(e.name) ? 'show' : 'hide'} it in every view`;
    const sw = el('span', 'lm-sw');
    sw.style.background = `var(--series-${e.slot})`;
    b.append(sw, el('span', null, e.short));
    b.onclick = () => { if (st.off.has(e.name)) st.off.delete(e.name); else st.off.add(e.name); saveState(); render(); };
    D.kinds.append(b);
  }
  D.newOnly.checked = st.newOnly;
  D.newOnly.disabled = !st.win;
  D.newOnlyWrap.classList.toggle('off', !st.win);
  D.newOnlyWrap.title = st.win ? 'Hide links (two hosts and an account) that already happened before this window starts'
    : 'Drag across the timeline first: over the whole case every link is new';
  D.chips.replaceChildren();
  const chip = (label, clear) => {
    const c = el('span', 'lm-chip', label);
    const x = el('button', null, '✕');
    x.type = 'button'; x.title = 'Clear'; x.setAttribute('aria-label', `Clear ${label}`);
    x.onclick = clear;
    c.append(x);
    D.chips.append(c);
  };
  if (st.account != null) chip(`Account: ${acctName(st.account)}`, () => { st.account = null; render(); });
  if (st.host != null) chip(`Host: ${HOSTS[st.host]}`, () => { st.host = null; render(); });
  for (const u of st.expected) chip(`Expected: ${shortAcct(u)}`, () => { st.expected.delete(u); saveState(); render(); });
  if (st.pathFrom != null) chip(`Path from ${HOSTS[st.pathFrom]}: right-click where it ends`, () => { st.pathFrom = null; render(); });
  if (st.routeHi && st.mode === 'chain') chip(`Route: ${st.routeHi.label}`, () => { st.routeHi = null; render(); });
  D.legend.replaceChildren();
  if (['chain', 'pivot', 'path'].includes(st.mode)) {
    const item = (svg, text) => { const s = el('span'); s.innerHTML = svg; s.append(text); D.legend.append(s); };
    item('<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M5 .5 9.5 5 5 9.5 .5 5Z" fill="var(--accent)"/></svg>', 'credential switch');
    const sess = EV.find((e) => e.session && ON.has(e.k));
    if (sess && st.mode === 'chain') {
      item(`<svg width="18" height="6" viewBox="0 0 18 6" aria-hidden="true"><rect y="1" width="18" height="4" rx="1" fill="var(--series-${sess.slot})" opacity=".6"/></svg>`, 'session');
    }
    if (st.jewels.size) item(`<svg width="11" height="11" viewBox="-5.5 -5.5 11 11" aria-hidden="true"><path d="${STAR_PATH}" fill="var(--accent)"/></svg>`, 'crown jewel');
  }
}

function paintStatus(text) {
  if (text != null) { D.status.textContent = text; return; }
  if (S.busy) D.status.textContent = 'Reading the movement events…';
}

/* ========================================================== stage helpers */

/* One canvas the height of the stage, kept in view by position: sticky,
   over a spacer as tall as what is drawn: a view of a thousand hosts is
   drawn a screen at a time (sy is how far down the screen starts), where a
   canvas that tall would pass the browser's limit on a canvas's size. */
function canvasFor(totalH) {
  const stage = D.stage;
  let c = stage.firstElementChild, sp = c && c.nextElementSibling;
  if (!c || c.tagName !== 'CANVAS' || !sp || stage.children.length !== 2) {
    c = ctx.el('canvas'); sp = ctx.el('div', 'lm-spacer');
    stage.replaceChildren(c, sp);
  }
  const W = stage.clientWidth, H = stage.clientHeight, dpr = window.devicePixelRatio || 1;
  sp.style.height = `${Math.max(0, totalH - H)}px`;
  c.style.width = `${W}px`; c.style.height = `${H}px`;
  if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) {
    c.width = Math.round(W * dpr); c.height = Math.round(H * dpr);
  }
  const g = c.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  const sy = Math.max(0, Math.min(stage.scrollTop, totalH - H));
  return { c, g, W, H, sy, dpr };
}
// Text is measured on a canvas of its own: sizing the one on screen to
// measure with would shrink its spacer and throw away the scroll position.
let measureCtx = null;
const measure = () => measureCtx || (measureCtx = document.createElement('canvas').getContext('2d'));
function emptyStage(msg, extra = null) {
  hit = null;
  const p = ctx.el('div', 'lm-empty');
  p.append(ctx.el('p', null, msg));
  if (extra) p.append(extra);
  D.stage.replaceChildren(p);
}
function showTip(html, x, y) {
  const tip = D.tip;
  tip.innerHTML = html;
  tip.hidden = false;
  const W = D.wrap.clientWidth, Hh = D.wrap.clientHeight, tw = tip.offsetWidth, th = tip.offsetHeight;
  let left = x + 14, top = y + 14;
  if (left + tw > W - 6) left = Math.max(6, x - tw - 14);
  if (top + th > Hh - 4) top = Math.max(4, y - th - 10);
  tip.style.left = `${left}px`; tip.style.top = `${top}px`;
}
const hideTip = () => { if (D) D.tip.hidden = true; };
function kindLines(kinds) {
  return kinds.map((n, k) => (n ? `<div class="k"><i style="background:${evColor(k)}"></i>${esc(EV[k].short)} ×${n.toLocaleString()}</div>` : '')).join('');
}
function linkTip(L) {
  const users = [...L.users.entries()].sort((a, b) => b[1] - a[1]);
  const acct = `<div class="d">${users.slice(0, 4).map(([u, n]) => esc(acctName(u)) + (users.length > 1 ? ` ×${n.toLocaleString()}` : '')).join('<br>')}${users.length > 4 ? `<br>and ${users.length - 4} more` : ''}</div>`;
  const when = L.first === L.last ? fmt(L.first) : `${fmt(L.first)} → ${fmt(L.last, dayOf(L.first) !== dayOf(L.last))}`;
  const fresh = st.win ? (L.fresh ? '<div class="new">New in this window</div>' : '<div class="d">Seen before this window</div>') : '';
  const also = L.also.map((n, k) => (n ? `<div class="d">Backed up by ${esc(EV[k].short)} ×${n.toLocaleString()}</div>` : '')).join('');
  return `<div class="t">${esc(HOSTS[L.s])} → ${esc(HOSTS[L.d])}</div>${acct}${kindLines(L.kinds)}${also}<div class="d">${when} · ${plural(L.n, 'row')}</div>${fresh}`;
}
const acctB = (u) => `<b>${esc(shortAcct(u))}</b>`;
function switchTip(x) {
  const came = x.fromAt == null ? '' : `${x.session ? 'on by a session' : 'arrived'} from ${esc(HOSTS[x.fromVia])} · ${fmt(x.fromAt)}`;
  if (x.how === 'explicit') {
    return `<div class="swt">Explicit credentials on ${esc(HOSTS[x.host])}</div>`
      + `<div>${acctB(x.from)} used ${acctB(x.to)}'s credentials to reach ${esc(HOSTS[x.dst])} · ${fmt(x.t)}</div>`
      + (came ? `<div class="d">${esc(shortAcct(x.from))} ${came}</div>` : '')
      + '<div class="d">Stated by the log (the acting account), not inferred.</div>';
  }
  return `<div class="swt">Credential switch on ${esc(HOSTS[x.host])}</div>`
    + `<div>In as ${acctB(x.from)} · ${came}</div>`
    + `<div>Out as ${acctB(x.to)} to ${esc(HOSTS[x.dst])} · ${fmt(x.t)}</div>`
    + (x.also.length ? `<div class="d">Also on this host shortly before: ${x.also.map((u) => esc(shortAcct(u))).join(', ')}</div>` : '')
    + `<div class="d">Inferred: ${esc(shortAcct(x.to))} never logged on to this host itself, and leaves it on no other day.</div>`;
}
function sessTip(x) {
  const end = x.b == null ? 'no close logged' : `${fmt(x.b, dayOf(x.a) !== dayOf(x.b))} (${dur(x.b - x.a)})`;
  return `<div class="t">Session on ${esc(HOSTS[x.d])}</div><div>${acctB(x.u)} from ${esc(HOSTS[x.s])}</div>`
    + `<div class="k"><i style="background:${evColor(x.k)}"></i>${esc(EV[x.k].short)}</div><div class="d">${fmt(x.a)} → ${end}</div>`;
}
const STAR_PATH = 'M0-5.2 1.5-1.6 5.2-1.6 2.2.8 3.3 4.6 0 2.4-3.3 4.6-2.2.8-5.2-1.6-1.5-1.6Z';
const starSvg = (fill = 'var(--accent)') => `<svg class="lm-star" width="11" height="11" viewBox="-5.5 -5.5 11 11" aria-label="crown jewel"><path d="${STAR_PATH}" fill="${fill}"/></svg>`;
function drawStar(g, x, y, fill) {
  g.save(); g.translate(x, y); g.fillStyle = fill; g.fill(new Path2D(STAR_PATH)); g.restore();
}
function hostTip(h) {
  const seen = new Map();
  for (const x of SPELL.get(h) || []) seen.set(x.raw, (seen.get(x.raw) || 0) + x.n);
  const al = [...seen].sort((a, b) => b[1] - a[1]).map(([v]) => v);
  return `<div class="t">${esc(HOSTS[h])}</div>`
    + (al.length ? `<div class="d">Seen as ${al.slice(0, 4).map((a) => `<span class="mono">${esc(a)}</span>`).join(', ')}${al.length > 4 ? ', …' : ''}</div>` : '')
    + '<div class="d">Right-click or click for actions</div>';
}
// A label cut to fit, with an ellipsis, at the canvas's current font.
function fit(g, text, w) {
  if (g.measureText(text).width <= w) return text;
  let lo = 0, hi = text.length;
  while (lo < hi) { const m = (lo + hi + 1) >> 1; if (g.measureText(text.slice(0, m) + '…').width <= w) lo = m; else hi = m - 1; }
  return text.slice(0, lo) + '…';
}
// Time-axis steps in minutes, the first that leaves room for a label.
const STEPS = [1, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080, 20160, 43200];
const stepFor = (span, px, room) => STEPS.find((s) => (span / s) * room <= px) || 43200;
function axisLabel(t, step) {
  const abs = T0M + t;
  return step >= 1440 || abs % 1440 === 0 ? fmtDay(t) : fmt(t, false);
}
// Ticks on whole steps of UTC time, not of minutes since the first hop.
function ticks(a, b, step) {
  const out = [];
  for (let abs = Math.ceil((T0M + a) / step) * step; abs <= T0M + b; abs += step) out.push(abs - T0M);
  return out;
}

/* ================================================================= chain */

function drawChain(F) {
  const cur = hit && hit.kind === 'chain' ? hit.cur : null;
  const L = F.links || (F.links = aggregate(F.hops, linkKey, F.first));
  if (!L.length) return emptyStage('Nothing in this window with these filters.');
  const firstAt = new Map();
  for (const l of L) for (const h of [l.s, l.d]) if (!firstAt.has(h) || l.first < firstAt.get(h)) firstAt.set(h, l.first);
  const lanes = [...firstAt.keys()].sort((a, b) => firstAt.get(a) - firstAt.get(b) || HOSTS[a].localeCompare(HOSTS[b]));
  const top = 30, H0 = D.stage.clientHeight;
  const laneH = Math.max(14, Math.min(26, (H0 - top - 12) / lanes.length));
  const totalH = Math.max(H0, top + lanes.length * laneH + 12);
  const { c, g, W, H, sy } = canvasFor(totalH);
  g.font = `11.5px ${PAL.mono}`;
  let widest = 0;
  for (const h of lanes) widest = Math.max(widest, g.measureText(HOSTS[h]).width);
  const x0 = Math.round(Math.max(90, Math.min(220, widest + 34))), x1 = W - 18;
  const X = (t) => x0 + ((t - F.a) / Math.max(1, F.b - F.a)) * (x1 - x0);
  const laneIdx = new Map(lanes.map((h, i) => [h, i]));
  const Y = (h) => top + laneIdx.get(h) * laneH + laneH / 2;
  const vis = (y0, y1) => y1 >= sy + top - 8 && y0 <= sy + H + 8;
  const span = F.b - F.a, step = stepFor(span, x1 - x0, 84);

  g.save();
  g.translate(0, -sy);
  // time grid
  for (const t of ticks(F.a, F.b, step)) {
    const x = Math.round(X(t)) + 0.5;
    g.strokeStyle = (T0M + t) % 1440 === 0 ? PAL.line2 : PAL.line; g.lineWidth = 1;
    g.beginPath(); g.moveTo(x, sy + top - 6); g.lineTo(x, sy + H); g.stroke();
  }
  // lanes
  g.textBaseline = 'middle';
  const lab = {};
  const i0 = Math.max(0, Math.floor((sy - laneH) / laneH) - 1), i1 = Math.min(lanes.length, Math.ceil((sy + H) / laneH) + 1);
  for (let i = i0; i < i1; i++) {
    const h = lanes[i], y = top + i * laneH + laneH / 2;
    const on = st.hoverHost === h || st.host === h;
    g.strokeStyle = on ? PAL.accent : PAL.line; g.globalAlpha = on ? 0.6 : 1;
    g.beginPath(); g.moveTo(x0 - 4, Math.round(y) + 0.5); g.lineTo(x1, Math.round(y) + 0.5); g.stroke();
    g.globalAlpha = 1;
    g.fillStyle = on ? PAL.accent : PAL.text;
    g.font = `${laneH < 16 ? 10.5 : 11.5}px ${PAL.mono}`;
    g.fillText(fit(g, HOSTS[h], x0 - 30), 10, y);
    if (st.jewels.has(h)) drawStar(g, x0 - 14, y, PAL.accent);
    if (y - sy >= top && y - sy <= H) lab[HOSTS[h]] = { x: 20, y: y - sy };
  }
  // sessions under their destination's lane, open to close; overlapping
  // ones take the next slot down
  const sessDrawn = [], slots = new Map();
  for (const x of F.sess) {
    if (!laneIdx.has(x.d)) continue;
    const y0 = Y(x.d);
    if (!vis(y0, y0 + 10)) continue;
    const fresh = !st.win || (F.first.get(linkKey(x)) ?? x.a) >= F.a;
    if (st.newOnly && st.win && !fresh) continue;
    const end = x.b ?? x.a + OPEN_TAIL;
    const xa = X(Math.max(x.a, F.a)), xb = Math.max(xa + 2, X(Math.min(end, F.b)));
    const used = slots.get(x.d) || [[], []];
    let slot = used.findIndex((s) => s.every(([p, q]) => xb < p - 1 || xa > q + 1));
    if (slot < 0) slot = 1;
    used[slot].push([xa, xb]); slots.set(x.d, used);
    const y = y0 + 2 + slot * 4;
    let alpha = fresh ? 0.6 : 0.22;
    if (st.hoverAcct != null && x.u !== st.hoverAcct) alpha *= 0.3;
    if (st.routeHi) alpha *= 0.4;
    if (cur && cur.sess === x) alpha = 0.95;
    const col = evColor(x.k);
    if (x.b == null) {
      const gr = g.createLinearGradient(xa, 0, xb, 0);
      gr.addColorStop(0, rgba(col, alpha)); gr.addColorStop(1, rgba(col, 0));
      g.fillStyle = gr;
    } else { g.fillStyle = col; g.globalAlpha = alpha; }
    g.fillRect(xa, y, xb - xa, 3);
    g.globalAlpha = 1;
    sessDrawn.push({ x, xa, xb, y });
  }
  // arrows: seen-before first, faded; new ones over them
  const arrows = L.map((l) => ({ l, x: X(l.first), ya: Y(l.s), yb: Y(l.d) }));
  const onRoute = (l) => !!st.routeHi && st.routeHi.keys.has(linkKey(l));
  const order = arrows.filter((a) => vis(Math.min(a.ya, a.yb), Math.max(a.ya, a.yb)))
    .sort((p, q) => (onRoute(p.l) - onRoute(q.l)) || (p.l.fresh - q.l.fresh) || (p.l.first - q.l.first));
  for (const a of order) {
    const faded = st.win && !a.l.fresh;
    const other = st.hoverAcct != null && a.l.u !== st.hoverAcct;
    const isHover = !!(cur && cur.link) && linkKey(cur.link) === linkKey(a.l), routed = onRoute(a.l);
    let alpha = faded && !routed ? 0.28 : 0.95;
    if (other) alpha *= 0.18;
    if (st.routeHi && !routed) alpha *= 0.15;
    g.globalAlpha = alpha;
    const col = isHover || routed ? PAL.accent : evColor(telling(a.l.kinds));
    g.strokeStyle = col; g.fillStyle = col;
    g.lineWidth = isHover ? 2.5 : routed ? 2.6 : (st.hoverAcct != null && !other ? 2 : 1.4);
    const dir = a.yb > a.ya ? 1 : -1;
    g.beginPath(); g.moveTo(a.x, a.ya); g.lineTo(a.x, a.yb - dir * 5); g.stroke();
    g.beginPath(); g.moveTo(a.x, a.yb); g.lineTo(a.x - 3.6, a.yb - dir * 7); g.lineTo(a.x + 3.6, a.yb - dir * 7); g.closePath(); g.fill();
    g.beginPath(); g.arc(a.x, a.ya, 2.4, 0, Math.PI * 2); g.fill();
  }
  g.globalAlpha = 1;
  // credential switches: a dashed run along the host's lane from the
  // arrival to the departure, and a diamond where the other account leaves
  const swDrawn = [];
  for (const x of F.swShown) {
    if (!laneIdx.has(x.host)) continue;
    const y = Y(x.host);
    if (!vis(y - 8, y + 8)) continue;
    const xt = X(x.t), xf = x.fromAt == null ? xt : X(Math.max(x.fromAt, F.a));
    const on = cur && cur.sw === x;
    g.globalAlpha = st.hoverAcct != null && st.hoverAcct !== x.to && st.hoverAcct !== x.from ? 0.25 : 1;
    g.strokeStyle = PAL.accent; g.lineWidth = on ? 2.5 : 1.8; g.setLineDash([3, 3]);
    g.beginPath(); g.moveTo(xf, y); g.lineTo(xt, y); g.stroke(); g.setLineDash([]);
    const r = on ? 6.5 : 5;
    g.fillStyle = PAL.accent; g.strokeStyle = PAL.ink; g.lineWidth = 1.5;
    g.beginPath(); g.moveTo(xt, y - r); g.lineTo(xt + r, y); g.lineTo(xt, y + r); g.lineTo(xt - r, y); g.closePath(); g.fill(); g.stroke();
    swDrawn.push({ x, xt, y });
  }
  g.globalAlpha = 1;
  g.restore();
  // the time axis stays put over the lanes as they scroll
  g.fillStyle = PAL.ink; g.fillRect(0, 0, W, top - 4);
  g.font = `11px ${PAL.mono}`; g.textBaseline = 'middle';
  for (const t of ticks(F.a, F.b, step)) {
    g.fillStyle = (T0M + t) % 1440 === 0 ? PAL.text : PAL.dim;
    g.fillText(axisLabel(t, step), Math.min(X(t) + 4, x1 - 40), 13);
  }
  c.lmHosts = lab;   // where each visible lane's label is, for tests and tooling
  hit = {
    kind: 'chain', cur,
    at(mx, my, vy) {
      if (vy < top - 4) return null;
      if (mx < x0 - 6) {
        const i = Math.floor((my - top) / laneH);
        return i >= 0 && i < lanes.length ? { host: lanes[i] } : null;
      }
      for (const s of swDrawn) if (Math.abs(mx - s.xt) <= 7 && Math.abs(my - s.y) <= 7) return { sw: s.x };
      let best = null, bd = 5;
      for (const a of arrows) {
        const dx = Math.abs(mx - a.x);
        if (dx > bd) continue;
        if (my < Math.min(a.ya, a.yb) - 4 || my > Math.max(a.ya, a.yb) + 4) continue;
        if (st.hoverAcct != null && a.l.u !== st.hoverAcct && best) continue;
        best = a; bd = dx;
      }
      if (best) return { link: best.l };
      for (const s of sessDrawn) if (mx >= s.xa - 2 && mx <= s.xb + 2 && my >= s.y - 2 && my <= s.y + 5) return { sess: s.x };
      return null;
    },
  };
  F.shown = { hosts: lanes.length, links: L.length, switches: F.swShown.length };
}

/* ================================================================ matrix */

function drawMatrix(F) {
  const P = F.pairs || (F.pairs = aggregate(F.hops, (h) => `${h.s}|${h.d}`, F.first));
  if (!P.length) return emptyStage('Nothing in this window with these filters.');
  const outs = new Map(), ins = new Map(), outN = new Map(), inN = new Map();
  for (const p of P) {
    outs.set(p.s, (outs.get(p.s) || 0) + 1); ins.set(p.d, (ins.get(p.d) || 0) + 1);
    outN.set(p.s, (outN.get(p.s) || 0) + p.n); inN.set(p.d, (inN.get(p.d) || 0) + p.n);
  }
  const byFan = (m, n) => (a, b) => m.get(b) - m.get(a) || n.get(b) - n.get(a) || HOSTS[a].localeCompare(HOSTS[b]);
  const allRows = [...outs.keys()].sort(byFan(outs, outN));
  const allCols = [...ins.keys()].sort(byFan(ins, inN));
  const W0 = D.stage.clientWidth;
  const probe = measure();
  probe.font = `11px ${PAL.mono}`;
  let widest = 0;
  for (const h of allRows.slice(0, MATRIX_ROWS)) widest = Math.max(widest, probe.measureText(HOSTS[h]).width);
  const labW = Math.round(Math.max(80, Math.min(200, widest + 26))), topH = 84;
  // every column at least 9px wide, and as many as fit: the busiest
  const cols = allCols.slice(0, Math.max(1, Math.floor((W0 - labW - 24) / 9)));
  const rows = allRows.slice(0, MATRIX_ROWS);
  const cs = Math.max(9, Math.min(26, Math.floor((W0 - labW - 24) / cols.length)));
  const { c, g, W, H, sy } = canvasFor(Math.max(D.stage.clientHeight, topH + rows.length * cs + 16));
  const ri = new Map(rows.map((h, i) => [h, i])), ci = new Map(cols.map((h, i) => [h, i]));
  const max = Math.max(...P.map((p) => p.n));
  const hov = hit && hit.kind === 'matrix' ? hit.cur : null;
  const r0 = Math.max(0, Math.floor(sy / cs) - 1), r1 = Math.min(rows.length, Math.ceil((sy + H) / cs) + 1);
  g.save();
  g.translate(0, -sy);
  if (hov && hov.cell && ri.has(hov.cell.s) && ci.has(hov.cell.d)) {
    g.fillStyle = PAL.accent; g.globalAlpha = 0.14;
    g.fillRect(labW, topH + ri.get(hov.cell.s) * cs, cols.length * cs, cs);
    g.fillRect(labW + ci.get(hov.cell.d) * cs, topH, cs, rows.length * cs);
    g.globalAlpha = 1;
  }
  g.fillStyle = PAL.panel2;
  for (let r = r0; r < r1; r++) for (let q = 0; q < cols.length; q++) g.fillRect(labW + q * cs + 1, topH + r * cs + 1, cs - 2, cs - 2);
  for (const p of P) {
    if (!ri.has(p.s) || !ci.has(p.d)) continue;
    const r = ri.get(p.s);
    if (r < r0 || r >= r1) continue;
    const x = labW + ci.get(p.d) * cs, y = topH + r * cs;
    const other = st.hoverAcct != null && !p.users.has(st.hoverAcct);
    g.globalAlpha = (0.3 + 0.7 * Math.log1p(p.n) / Math.log1p(max)) * (other ? 0.2 : 1);
    g.fillStyle = evColor(telling(p.kinds));
    g.fillRect(x + 1, y + 1, cs - 2, cs - 2);
    g.globalAlpha = 1;
    if (p.fresh) { g.strokeStyle = PAL.accent; g.lineWidth = 1.6; g.strokeRect(x + 1.5, y + 1.5, cs - 3, cs - 3); }
  }
  const fs = cs < 12 ? 10 : 11;
  g.font = `${fs}px ${PAL.mono}`; g.textBaseline = 'middle';
  const lab = {};
  for (let i = r0; i < r1; i++) {
    const h = rows[i], y = topH + i * cs + cs / 2;
    const on = (hov && (hov.host === h || (hov.cell && hov.cell.s === h))) || st.host === h;
    g.fillStyle = on ? PAL.accent : PAL.text; g.textAlign = 'right';
    const name = fit(g, HOSTS[h], labW - 22);
    g.fillText(name, labW - 6, y);
    if (st.jewels.has(h)) drawStar(g, labW - 6 - g.measureText(name).width - 9, y, PAL.accent);
    if (y - sy >= topH && y - sy <= H) lab[HOSTS[h]] = { x: labW - 12, y: y - sy };
  }
  g.restore();
  // the destination labels stay put over the rows as they scroll
  g.fillStyle = PAL.ink; g.fillRect(0, 0, W, topH - 1);
  g.font = `${fs}px ${PAL.mono}`; g.textBaseline = 'middle';
  cols.forEach((h, i) => {
    const on = (hov && (hov.host === h || (hov.cell && hov.cell.d === h))) || st.host === h;
    g.save(); g.translate(labW + i * cs + cs / 2, topH - 6); g.rotate(-Math.PI / 3);
    const jw = st.jewels.has(h);
    if (jw) drawStar(g, 5, 0, PAL.accent);
    g.fillStyle = on ? PAL.accent : PAL.text; g.textAlign = 'left';
    g.fillText(fit(g, HOSTS[h], 88), jw ? 13 : 0, 0);
    g.restore();
    // a point on the slanted label, where at() below finds this column
    if (!lab[HOSTS[h]]) lab[HOSTS[h]] = { x: labW + i * cs + cs / 2 + 4, y: topH - 10 };
  });
  g.textAlign = 'left';
  g.fillStyle = PAL.dim; g.font = `11px ${PAL.ui}`;
  g.fillText('from ↓   to →', 8, topH - 10);
  c.lmHosts = lab;
  const byCell = new Map(P.map((p) => [`${p.s}|${p.d}`, p]));
  hit = {
    kind: 'matrix', cur: hov,
    at(mx, my, vy) {
      if (vy < topH) {
        if (mx <= labW) return null;
        // rotated labels: walk back along the 60° slant to the column it starts from
        const q = Math.floor((mx - labW - (topH - 6 - vy) / Math.tan(Math.PI / 3)) / cs);
        return cols[q] != null ? { host: cols[q] } : null;
      }
      if (mx < labW) { const i = Math.floor((my - topH) / cs); return rows[i] != null ? { host: rows[i] } : null; }
      const q = Math.floor((mx - labW) / cs), r = Math.floor((my - topH) / cs);
      if (q < 0 || r < 0 || q >= cols.length || r >= rows.length) return null;
      const p = byCell.get(`${rows[r]}|${cols[q]}`);
      return p ? { cell: p, link: p } : null;
    },
  };
  F.shown = { hosts: new Set([...allRows, ...allCols]).size, links: P.length,
    cut: rows.length < allRows.length || cols.length < allCols.length
      ? `the busiest ${rows.length} of ${allRows.length} sources and ${cols.length} of ${allCols.length} destinations` : '' };
}

/* ============================================================ by account */

function sparkBin() {
  for (const b of [60, 120, 180, 360, 720, 1440, 2880, 10080, 20160]) if (SPAN / b <= 96) return b;
  return 43200;
}
function accountRows(F) {
  // Server-ness from the whole case: a host many different accounts log on
  // to is infrastructure; a hop between two hosts that are not is
  // workstation to workstation.
  const intoBy = new Map();
  for (const h of F.base) {
    if (h.u === NOACCT) continue;
    if (!intoBy.has(h.d)) intoBy.set(h.d, new Set());
    intoBy.get(h.d).add(h.u);
  }
  const server = (h) => (intoBy.get(h) || new Set()).size >= 3;
  const daysOf = new Map(), allDays = new Map(), bins = new Map();
  const bin = sparkBin(), nb = Math.max(1, Math.ceil(SPAN / bin));
  for (const h of F.base) {
    if (h.u === NOACCT) continue;
    const k = linkKey(h);
    if (!daysOf.has(k)) daysOf.set(k, new Set());
    daysOf.get(k).add(dayOf(h.t));
    if (!allDays.has(h.u)) allDays.set(h.u, new Set());
    allDays.get(h.u).add(dayOf(h.t));
    if (!bins.has(h.u)) bins.set(h.u, new Array(nb).fill(0));
    bins.get(h.u)[Math.min(nb - 1, Math.floor(h.t / bin))] += h.n;
  }
  const by = new Map();
  for (const h of F.inWin) {
    if (h.u === NOACCT) continue;
    if (!by.has(h.u)) by.set(h.u, []);
    by.get(h.u).push(h);
  }
  const swTo = new Map(), swFrom = new Map();
  for (const x of F.sw) {
    if (!swTo.has(x.to)) swTo.set(x.to, []); swTo.get(x.to).push(x);
    if (!swFrom.has(x.from)) swFrom.set(x.from, []); swFrom.get(x.from).push(x);
  }
  const multiDay = caseDays() > 1;
  const out = [];
  for (const [u, hs] of by) {
    const dsts = new Set(hs.map((h) => h.d)), srcs = new Set(hs.map((h) => h.s));
    let n = 0, first = Infinity, last = -Infinity;
    for (const h of hs) { n += h.n; if (h.t < first) first = h.t; if (h.t > last) last = h.t; }
    const flags = [];
    for (const x of swTo.get(u) || []) {
      flags.push(['hot', `switched from ${shortAcct(x.from)}`, x.how === 'explicit'
        ? `${shortAcct(x.from)} used these credentials on ${HOSTS[x.host]} to reach ${HOSTS[x.dst]}`
        : `Left ${HOSTS[x.host]} soon after ${shortAcct(x.from)} arrived there, without ever logging on there itself`, 3]);
    }
    for (const x of swFrom.get(u) || []) {
      flags.push(['hot', `switched to ${shortAcct(x.to)}`, x.how === 'explicit'
        ? `Used ${shortAcct(x.to)}'s credentials on ${HOSTS[x.host]}`
        : `Arrived on ${HOSTS[x.host]}, and ${shortAcct(x.to)} left it soon after`, 2]);
    }
    const statedSwitch = (swTo.get(u) || []).some((x) => x.how === 'explicit');
    // a sweep: from one source, hosts first reached within 15 minutes that
    // the pair touches on no other day (a nightly push repeats daily)
    let sweep = 0;
    const bySrc = new Map();
    for (const h of hs) {
      if (multiDay && daysOf.get(linkKey(h)).size !== 1) continue;
      if (!bySrc.has(h.s)) bySrc.set(h.s, new Map());
      const m = bySrc.get(h.s);
      if (!m.has(h.d) || h.t < m.get(h.d)) m.set(h.d, h.t);
    }
    for (const m of bySrc.values()) {
      const ts = [...m.values()].sort((a, b) => a - b);
      for (let i = 0, j = 0; i < ts.length; i++) { while (ts[i] - ts[j] > 15) j++; sweep = Math.max(sweep, i - j + 1); }
    }
    if (sweep >= 4) flags.push(['hot', `sweep ×${sweep}`, `Reached ${sweep} hosts from one source within 15 minutes, hosts it touches on no other day`, 3]);
    // failed attempts, then a success on the same pair within ten minutes
    const ok = new Map();
    for (const h of hs) if (SUCCEEDED.has(h.k)) { const k = `${h.s}|${h.d}`; if (!ok.has(k)) ok.set(k, []); ok.get(k).push(h.t); }
    if (hs.some((f) => EV[f.k].fails && (ok.get(`${f.s}|${f.d}`) || []).some((t) => t >= f.t && t - f.t <= 10))) {
      flags.push(['hot', 'fails, then logs on', 'Failed attempts followed by a success on the same host within 10 minutes', 2]);
    }
    if (!statedSwitch && hs.some((h) => EV[h.k].explicit)) flags.push(['hot', 'explicit credentials', 'Named as the account an explicit-credential event used', 2]);
    const p2p = hs.filter((h) => !server(h.s) && !server(h.d));
    if (p2p.length) flags.push(['', 'workstation → workstation', `${plural(p2p.length, 'hop')} between two hosts that few accounts log on to`, 2]);
    if (multiDay && (allDays.get(u) || new Set()).size === 1 && dsts.size >= 3) flags.push(['', 'one day only', 'All of its movement in the case happens on a single day', 1]);
    if (dsts.size >= 10) flags.push(['', `${dsts.size} hosts`, 'Reached ten or more hosts', 1]);
    out.push({ u, dsts: dsts.size, srcs: srcs.size, n, first, last, flags, bins: bins.get(u) || [], score: flags.reduce((a, f) => a + f[3], 0) });
  }
  return { rows: out.sort((a, b) => b.score - a.score || b.dsts - a.dsts || b.n - a.n || USERS[a.u].localeCompare(USERS[b.u])), bin };
}
// Hops per bin across the case, after LogonTracer's per-account timeline:
// a burst on one afternoon reads at a glance next to a flat daily habit.
const SPARK_W = 2, SPARK_H = 18;
function spark(bins, bin, F) {
  const max = Math.max(1, ...bins);
  let pin = '', pout = '';
  bins.forEach((v, i) => {
    if (!v) return;
    const h = Math.max(1.5, Math.sqrt(v / max) * (SPARK_H - 1));
    const seg = `M${i * SPARK_W} ${(SPARK_H - h).toFixed(1)}h${SPARK_W - 0.5}V${SPARK_H}h-${SPARK_W - 0.5}Z`;
    const mid = i * bin + bin / 2;
    if (!st.win || (mid >= F.a - bin / 2 && mid <= F.b + bin / 2)) pin += seg; else pout += seg;
  });
  const w = Math.max(1, bins.length * SPARK_W);
  let tk = '';
  if (bin < 1440 && caseDays() <= 31) {
    for (let abs = Math.ceil(T0M / 1440) * 1440; abs < T0M + SPAN; abs += 1440) {
      const x = ((abs - T0M) / bin) * SPARK_W;
      if (x > 0) tk += `M${x.toFixed(1)} 0V${SPARK_H}`;
    }
  }
  return `<svg class="lm-spark" width="${w}" height="${SPARK_H}" viewBox="0 0 ${w} ${SPARK_H}" aria-hidden="true">`
    + `<path d="${tk}" stroke="var(--line-2)" stroke-width="1"/><path class="out" d="${pout}"/><path class="${st.win ? 'in' : 'all'}" d="${pin}"/></svg>`;
}
function drawAccounts(F) {
  hit = null;
  const { el } = ctx;
  const { rows, bin } = F.acct || (F.acct = accountRows(F));
  if (!rows.length) {
    const flows = F.inWin.length && F.inWin.every((h) => h.u === NOACCT);
    return emptyStage(flows ? 'The hops in this window carry no account: the ticked events read no user column.'
      : 'No accounts in this window with these filters.');
  }
  const wrap = el('div', 'lm-acct-wrap');
  const t = el('table', 'lm-acct');
  const head = el('tr');
  const binText = bin >= 1440 ? `${bin / 1440} day${bin === 1440 ? '' : 's'}` : `${bin / 60} h`;
  for (const [h, cls] of [['', ''], ['Account', ''], ['Why it stands out', ''], [`Activity per ${binText}, ${fmtDay(0)} → ${fmtDay(SPAN - 1)}`, ''],
    ['Hosts reached', 'num'], ['From', 'num'], ['Rows', 'num'], ['First', ''], ['Last', ''], ['', '']]) head.append(el('th', cls, h));
  const thead = el('thead');
  thead.append(head);
  const body = el('tbody');
  rows.slice(0, ACCOUNT_ROWS).forEach((r, i) => {
    const tr = el('tr');
    tr.dataset.account = USERS[r.u];
    if (st.account === r.u) tr.className = 'sel';
    const [dom, name] = splitAcct(USERS[r.u]);
    const nm = el('td', 'name');
    nm.append(el('span', 'dom', dom), document.createTextNode(name));
    const why = el('td', 'why');
    if (r.flags.length) {
      for (const [cls, label, title] of r.flags) { const f = el('span', `lm-flag ${cls}`.trim(), label); f.title = title; why.append(f); }
    } else why.append(el('span', 'quiet', 'nothing unusual'));
    const go = el('td');
    const b = el('button', 'lm-link', 'Show in Chain ›');
    b.type = 'button';
    b.onclick = (e) => { e.stopPropagation(); st.account = r.u; st.mode = 'chain'; render(); };
    go.append(b);
    const act = el('td', 'act');
    act.innerHTML = spark(r.bins, bin, F);
    tr.append(el('td', 'rank', String(i + 1)), nm, why, act, el('td', 'num', String(r.dsts)), el('td', 'num', String(r.srcs)),
      el('td', 'num', r.n.toLocaleString()), el('td', null, fmt(r.first)), el('td', null, fmt(r.last)), go);
    tr.onclick = () => { st.account = st.account === r.u ? null : r.u; render(); };
    tr.oncontextmenu = (e) => { e.preventDefault(); openMenu({ account: r.u }, e); };
    body.append(tr);
  });
  t.append(thead, body);
  wrap.append(t);
  if (rows.length > ACCOUNT_ROWS) wrap.append(el('p', 'lm-more', `and ${plural(rows.length - ACCOUNT_ROWS, 'more account')}, with less that stands out`));
  D.stage.replaceChildren(wrap);
  F.shown = { accounts: rows.length };
}

/* ================================================================= pivot */

// Which hosts sit how many hops before and after the pivot, worked out
// once per computation rather than on every hover.
function pivotPlacement(F, f) {
  if (F.pv && F.pv.f === f) return F.pv;
  const outBy = new Map(), inBy = new Map(), pairKinds = new Map();
  for (const h of F.hops) {
    const pk = `${h.s}|${h.d}`;
    if (!pairKinds.has(pk)) pairKinds.set(pk, EV.map(() => 0));
    pairKinds.get(pk)[h.k] += h.n;
    if (!outBy.has(h.s)) outBy.set(h.s, []);
    outBy.get(h.s).push(h);
    if (!inBy.has(h.d)) inBy.set(h.d, []);
    inBy.get(h.d).push(h);
  }
  // Each side a level at a time, nearest first and alternating: a busy
  // host's next hops can reach every host in the case within three, and
  // filled out first they left nothing to have come from.
  const placed = new Map([[f, { lvl: 0, t: null, via: null }]]);
  const grow = (frontier, lvl) => {
    const fwd = lvl > 0, next = new Map();
    for (const x of frontier) {
      const tx = placed.get(x).t;   // null for the pivot: any time at all
      for (const h of (fwd ? outBy : inBy).get(x) || []) {
        const y = fwd ? h.d : h.s;
        // downstream, each next hop no earlier than the hop that reached
        // its source; upstream, each earlier hop no later than the one it
        // led to
        if ((tx != null && (fwd ? h.t < tx : h.t > tx)) || placed.has(y)) continue;
        const cur = next.get(y);
        if (!cur || (fwd ? h.t < cur.t : h.t > cur.t)) next.set(y, { lvl, t: h.t, via: h });
      }
    }
    for (const [y, v] of next) placed.set(y, v);
    return [...next.keys()];
  };
  let down = [f], up = [f];
  for (const lvl of [1, -1, 2, -2, 3]) {
    if (lvl > 0 && down.length) down = grow(down, lvl);
    if (lvl < 0 && up.length) up = grow(up, lvl);
  }
  F.pv = { f, placed, pairKinds };
  return F.pv;
}

function drawPivot(F) {
  const f = st.pivot;
  const { placed, pairKinds } = pivotPlacement(F, f);
  const levels = [...new Set([...placed.values()].map((v) => v.lvl))].sort((a, b) => a - b);
  const boxH = 32, gap = 6, slot = boxH + gap, top = 40;
  const colOf = (lv) => [...placed.entries()].filter(([, v]) => v.lvl === lv).sort((a, b) => (a[1].t ?? 0) - (b[1].t ?? 0));
  // The two columns next to the pivot stack around it; anything further
  // out sits level with the host that reached it (or that it reached), so
  // a chain reads as a straight line rather than a fan of crossings.
  const near = Math.max(1, Math.min(PIVOT_CAP, colOf(1).length), Math.min(PIVOT_CAP, colOf(-1).length));
  const midY = top + ((near - 1) * slot) / 2;
  const yOf = new Map([[f, midY]]);
  const extra = new Map();
  for (const lv of [1, -1, 2, -2, 3]) {
    const col = colOf(lv);
    if (!col.length) continue;
    const taken = [];
    const free = (y) => !taken.some((t) => Math.abs(t - y) < slot - 0.5);
    col.forEach(([h, v], i) => {
      if (i >= PIVOT_CAP) return;
      let y;
      if (Math.abs(lv) === 1) y = midY - ((Math.min(col.length, PIVOT_CAP) - 1) * slot) / 2 + i * slot;
      else {
        const parent = lv > 0 ? v.via.s : v.via.d;
        y = yOf.has(parent) ? yOf.get(parent) : top;
        while (!free(y)) y += slot;
      }
      taken.push(y); yOf.set(h, y);
    });
    if (col.length > PIVOT_CAP) extra.set(lv, { n: col.length - PIVOT_CAP, y: Math.max(...taken) + slot });
  }
  const maxY = Math.max(...yOf.values(), ...[...extra.values()].map((e) => e.y));
  const { c, g, W, sy } = canvasFor(Math.max(D.stage.clientHeight, maxY + slot + 16));
  const colW = W / Math.max(levels.length, 1);
  const bw = Math.max(60, Math.min(176, colW - 34));
  const xOfLv = (lv) => levels.indexOf(lv) * colW + (colW - bw) / 2;
  const pos = new Map([...yOf].map(([h, y]) => [h, { x: xOfLv(placed.get(h).lvl), y }]));
  const hov = hit && hit.kind === 'pivot' ? hit.cur : null;
  g.save();
  g.translate(0, -sy);
  // edges: each host's first arrival, or for the upstream side its last departure
  const edges = [];
  for (const [h, v] of placed) {
    if (!v.via || !pos.has(h)) continue;
    const a = v.lvl > 0 ? v.via.s : h, b = v.lvl > 0 ? h : v.via.d;
    if (!pos.has(a) || !pos.has(b)) continue;
    const pa = pos.get(a), pb = pos.get(b);
    const e = { h: v.via, x1: pa.x + bw, y1: pa.y + boxH / 2, x2: pb.x, y2: pb.y + boxH / 2,
      sw: F.swShown.find((x) => x.host === v.via.s && x.to === v.via.u && x.t <= v.via.t + 1) };
    edges.push(e);
    const on = !!(hov && hov.edge) && hov.edge.h === e.h;
    const other = st.hoverAcct != null && v.via.u !== st.hoverAcct;
    g.globalAlpha = other ? 0.2 : 0.9;
    g.strokeStyle = on ? PAL.accent : evColor(telling(pairKinds.get(`${v.via.s}|${v.via.d}`)));
    g.lineWidth = on ? 2.5 : 1.6;
    const mx = (e.x1 + e.x2) / 2;
    g.beginPath(); g.moveTo(e.x1, e.y1); g.bezierCurveTo(mx, e.y1, mx, e.y2, e.x2 - 6, e.y2); g.stroke();
    g.fillStyle = g.strokeStyle;
    g.beginPath(); g.moveTo(e.x2, e.y2); g.lineTo(e.x2 - 7, e.y2 - 3.5); g.lineTo(e.x2 - 7, e.y2 + 3.5); g.closePath(); g.fill();
    if (e.sw) {
      const cx = e.x1 + 9, r = 5;
      g.fillStyle = PAL.accent; g.strokeStyle = PAL.ink; g.lineWidth = 1.5;
      g.beginPath(); g.moveTo(cx, e.y1 - r); g.lineTo(cx + r, e.y1); g.lineTo(cx, e.y1 + r); g.lineTo(cx - r, e.y1); g.closePath(); g.fill(); g.stroke();
    }
    g.globalAlpha = 1;
  }
  // boxes
  g.textBaseline = 'middle';
  const lab = {};
  for (const [h, p] of pos) {
    const v = placed.get(h), isF = h === f, on = hov && hov.host === h;
    g.fillStyle = isF ? PAL.accent : PAL.panel; g.strokeStyle = on ? PAL.accent : PAL.line2; g.lineWidth = on ? 2 : 1;
    g.fillRect(p.x, p.y, bw, boxH); g.strokeRect(p.x + 0.5, p.y + 0.5, bw - 1, boxH - 1);
    g.fillStyle = isF ? PAL.accentFg : PAL.text; g.font = `600 12px ${PAL.mono}`;
    g.fillText(fit(g, HOSTS[h], bw - 26), p.x + 8, p.y + 11);
    if (st.jewels.has(h)) drawStar(g, p.x + bw - 10, p.y + 10, isF ? PAL.accentFg : PAL.accent);
    g.font = `11px ${PAL.ui}`; g.fillStyle = isF ? PAL.accentFg : PAL.dim;
    const sub = isF ? 'right-click a host to move here' : `${fmt(v.t)} · ${shortAcct(v.via.u)}`;
    g.fillText(fit(g, sub, bw - 14), p.x + 8, p.y + 23);
    lab[HOSTS[h]] = { x: p.x + bw / 2, y: p.y + boxH / 2 - sy };
  }
  for (const [lv, e] of extra) { g.fillStyle = PAL.dim; g.font = `11px ${PAL.ui}`; g.fillText(`and ${e.n} more`, xOfLv(lv) + 8, e.y + 10); }
  if (placed.size === 1) { g.fillStyle = PAL.dim; g.font = `13px ${PAL.ui}`; g.fillText('No hops into or out of this host with these filters.', 16, midY + boxH + 30); }
  g.restore();
  g.fillStyle = PAL.ink; g.fillRect(0, 0, W, top - 12);
  g.font = `11px ${PAL.ui}`; g.textBaseline = 'middle'; g.fillStyle = PAL.dim; g.textAlign = 'center';
  levels.forEach((lv, i) => {
    const label = lv === 0 ? 'pivot' : lv < 0 ? `${-lv} hop${lv === -1 ? '' : 's'} before` : `${lv} hop${lv === 1 ? '' : 's'} after`;
    g.fillText(label.toUpperCase(), i * colW + colW / 2, 16);
  });
  g.textAlign = 'left';
  c.lmHosts = lab;
  hit = {
    kind: 'pivot', cur: hov,
    at(mx, my) {
      for (const [h, p] of pos) if (mx >= p.x && mx <= p.x + bw && my >= p.y && my <= p.y + boxH) return { host: h };
      let best = null, bd = 6;
      for (const e of edges) {
        for (let i = 0; i <= 20; i++) {
          const t = i / 20, u = 1 - t, mxp = (e.x1 + e.x2) / 2;
          const x = u * u * u * e.x1 + 3 * u * u * t * mxp + 3 * u * t * t * mxp + t * t * t * e.x2;
          const y = u * u * u * e.y1 + 3 * u * u * t * e.y1 + 3 * u * t * t * e.y2 + t * t * t * e.y2;
          const d = Math.hypot(mx - x, my - y);
          if (d < bd) { bd = d; best = e; }
        }
      }
      return best ? { edge: best } : null;
    },
  };
  F.shown = { hosts: placed.size };
}

/* ================================================================== path */

const DIAMOND = '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M6 .8 11.2 6 6 11.2 .8 6Z" fill="var(--accent)" stroke="var(--ink)" stroke-width="1.5"/></svg>';
function drawPath(F) {
  hit = null;
  const { el } = ctx;
  const P = st.path;
  const targets = P.jewels ? new Set([...st.jewels].filter((h) => h !== P.from)) : new Set([P.to]);
  if (!targets.size) { F.shown = { routes: 0 }; return emptyStage('No host is marked as a crown jewel. Right-click one to mark it.'); }
  const tName = [...targets].map((h) => HOSTS[h]).join(', ');
  const { routes, cut } = findRoutes(F, P.from, targets);
  F.shown = { routes: routes.length, best: routes[0] };
  const box = el('div', 'lm-routes'), tips = [];
  const tipOn = (node, html) => { node.dataset.tip = String(tips.push(html) - 1); };
  const intro = el('p', 'lm-intro');
  intro.innerHTML = `From <b>${esc(HOSTS[P.from])}</b> to <b>${esc(tName)}</b>${st.win ? ', inside the window' : ', across the whole case'}. `
    + `Each hop leaves no earlier than the one before it arrived, and within ${DWELL / 60} hours of it. `
    + 'Where the account changes, a credential switch has to explain the step; routes with fewer unexplained steps come first.';
  box.append(intro);
  if (cut) box.append(el('p', 'lm-intro', 'The search stopped early: there are too many hops to try every route. Drag across the timeline to search a window.'));
  if (!routes.length) box.append(el('p', 'lm-intro', 'No route with these filters. Try the whole case, or turn more event types back on.'));
  routes.forEach((r, ri) => {
    const sec = el('section', `lm-route${ri === 0 ? ' best' : ''}`);
    const head = el('header');
    const [gaps, unknown] = r.score, n = r.hops.length, t0 = r.hops[0].t, t1 = r.hops[n - 1].t;
    head.append(el('span', 'n', `Route ${ri + 1}`), el('span', null, plural(n, 'hop')),
      el('span', gaps ? 'bad' : unknown ? 'unk' : 'ok', gaps ? `${gaps} unexplained step${gaps === 1 ? '' : 's'}`
        : unknown ? `${unknown} step${unknown === 1 ? '' : 's'} with no account to check` : 'every step explained'),
      el('span', null, `${fmt(t0)} → ${fmt(t1, dayOf(t0) !== dayOf(t1))}`));
    const show = el('button', 'lm-link', 'Show in Chain ›');
    show.type = 'button';
    show.onclick = () => { st.routeHi = { keys: new Set(r.hops.map(linkKey)), label: r.hosts.map((h) => HOSTS[h]).join(' → ') }; st.mode = 'chain'; render(); };
    head.append(show);
    const steps = el('div', 'lm-steps');
    r.hosts.forEach((h, i) => {
      if (i > 0) {
        const hop = r.hops[i - 1];
        const hp = el('div', 'lm-hop');
        hp.style.setProperty('--c', `var(--series-${EV[hop.k].slot})`);
        hp.append(el('span', 'a', shortAcct(hop.u)), el('span', 'ln'),
          el('span', 't', fmt(hop.t, i === 1 || dayOf(hop.t) !== dayOf(r.hops[i - 2].t))));
        tipOn(hp, `<div class="t">${esc(HOSTS[hop.s])} → ${esc(HOSTS[hop.d])}</div><div class="d">${esc(acctName(hop.u))}</div>`
          + `<div class="k"><i style="background:${evColor(hop.k)}"></i>${esc(EV[hop.k].short)}</div><div class="d">${fmt(hop.t)}</div>`);
        steps.append(hp);
      }
      const hb = el('div', 'lm-hbox');
      hb.dataset.host = HOSTS[h];
      const nm = el('b', null, HOSTS[h]);
      if (st.jewels.has(h)) nm.insertAdjacentHTML('beforeend', starSvg(i === 0 ? 'var(--accent-fg)' : 'var(--accent)'));
      let sub;
      if (i === 0) { hb.classList.add('start'); sub = 'start'; tipOn(hb, hostTip(h)); }
      else if (i === r.hosts.length - 1) { sub = st.jewels.has(h) ? 'crown jewel' : 'destination'; tipOn(hb, hostTip(h)); }
      else {
        const s = r.steps[i - 1], inH = r.hops[i - 1], outH = r.hops[i], wait = dur(outH.t - inH.t);
        const inOut = `<div>In as ${acctB(inH.u)} from ${esc(HOSTS[inH.s])} · ${fmt(inH.t)}</div>`
          + `<div>Out as ${acctB(outH.u)} to ${esc(HOSTS[outH.d])} · ${fmt(outH.t)}</div>`;
        if (s === null) { sub = `same account · ${wait}`; tipOn(hb, hostTip(h)); }
        else if (s === 'unknown') {
          hb.classList.add('unk'); sub = `no account · ${wait}`;
          const bare = [inH, outH].filter((x) => x.u === NOACCT).map((x) => EV[x.k].short);
          tipOn(hb, `<div class="t">No account to check on ${esc(HOSTS[h])}</div>${inOut}`
            + `<div class="d">${esc([...new Set(bare)].join(', '))} names no account, so nothing here says whether the same credential went on. Ranked after explained steps, ahead of unexplained ones.</div>`);
        } else if (s === 'gap') {
          hb.classList.add('gap'); sub = `unexplained · ${wait}`;
          tipOn(hb, `<div class="t bad">Unexplained step on ${esc(HOSTS[h])}</div>${inOut}`
            + '<div class="d">Nothing in these events links the two accounts on this host: it may be two unrelated logons that happen to line up.</div>');
        } else {
          hb.classList.add('switch'); sub = `${s.how === 'explicit' ? 'stated switch' : 'credential switch'} · ${wait}`;
          hb.insertAdjacentHTML('beforeend', `<span class="mark">${DIAMOND}</span>`);
          tipOn(hb, switchTip(s));
        }
      }
      hb.prepend(nm, el('small', null, sub));
      hb.oncontextmenu = (e) => { e.preventDefault(); openMenu({ host: h }, e); };
      hb.onclick = (e) => openMenu({ host: h }, e);
      steps.append(hb);
    });
    sec.append(head, steps);
    box.append(sec);
  });
  box.addEventListener('mousemove', (ev) => {
    const t = ev.target.closest('[data-tip]');
    if (!t) return hideTip();
    const wr = D.wrap.getBoundingClientRect();
    showTip(tips[Number(t.dataset.tip)], ev.clientX - wr.left, ev.clientY - wr.top);
  });
  box.addEventListener('mouseleave', hideTip);
  D.stage.replaceChildren(box);
}

/* ============================================================ histogram */

let drag = null;
function histBin(w) {
  const cap = Math.max(20, Math.min(300, w / 3));
  for (const b of [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080]) if (SPAN / b <= cap) return b;
  return 43200;
}
function drawHist(F) {
  const hc = D.histCanvas;
  const dpr = window.devicePixelRatio || 1, w = hc.clientWidth, h = hc.clientHeight;
  if (!w || !h) return;
  hc.width = Math.round(w * dpr); hc.height = Math.round(h * dpr);
  const g = hc.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  const bin = histBin(w), N = Math.max(1, Math.ceil(SPAN / bin)), bw = w / (SPAN / bin);
  const bins = Array.from({ length: N }, () => EV.map(() => 0));
  for (const x of F.base) if (st.account == null || x.u === st.account) bins[Math.min(N - 1, Math.floor(x.t / bin))][x.k] += x.n;
  const max = Math.max(1, ...bins.map((b) => b.reduce((a, c) => a + c, 0)));
  const xOf = (m) => (m / SPAN) * w;
  bins.forEach((b, i) => {
    let y = h - 12;
    const mid = i * bin + bin / 2;
    const inWin = !st.win || (mid >= st.win[0] - bin / 2 && mid <= st.win[1] + bin / 2);
    b.forEach((v, k) => {
      if (!v) return;
      const bh = Math.max(1, (v / max) * (h - 16));
      g.fillStyle = evColor(k); g.globalAlpha = inWin ? 0.9 : 0.28;
      g.fillRect(i * bw + 0.5, y - bh, Math.max(1, bw - 1), bh);
      y -= bh;
    });
  });
  g.globalAlpha = 1;
  g.fillStyle = PAL.dim; g.font = `10.5px ${PAL.mono}`; g.textBaseline = 'alphabetic';
  const step = stepFor(SPAN, w, 90);
  for (const t of ticks(0, SPAN, step)) {
    g.fillRect(Math.round(xOf(t)), h - 12, 1, 12);
    g.fillText(axisLabel(t, step), xOf(t) + 4, h - 1);
  }
  if (st.win) {
    g.strokeStyle = PAL.accent; g.lineWidth = 1.5; g.fillStyle = PAL.accent;
    const a = xOf(st.win[0]), b = xOf(st.win[1]);
    g.globalAlpha = 0.14; g.fillRect(a, 0, Math.max(2, b - a), h - 12); g.globalAlpha = 1;
    g.strokeRect(a + 0.75, 0.75, Math.max(2, b - a) - 1.5, h - 13.5);
  }
  const per = bin >= 1440 ? `${bin / 1440} day${bin === 1440 ? '' : 's'}` : bin >= 60 ? `${bin / 60} h` : `${bin} min`;
  D.histLabel.textContent = st.win
    ? `Window ${fmt(st.win[0])} → ${fmt(st.win[1], dayOf(st.win[0]) !== dayOf(st.win[1]))} · drag again to change, click to clear`
    : `${fmt(0)} → ${fmt(SPAN - 1)} · rows per ${per} · drag across to zoom in`;
  D.whole.hidden = !st.win;
}
function wireHist() {
  const hc = D.histCanvas;
  const minuteAt = (ev) => {
    const r = hc.getBoundingClientRect();
    return Math.round(Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width)) * SPAN);
  };
  hc.addEventListener('pointerdown', (ev) => { if (!HOPS.length) return; drag = { m: minuteAt(ev), x: ev.clientX, moved: false }; hc.setPointerCapture(ev.pointerId); });
  hc.addEventListener('pointermove', (ev) => {
    if (!drag) return;
    if (Math.abs(ev.clientX - drag.x) > 3) drag.moved = true;
    if (!drag.moved) return;
    const m = minuteAt(ev);
    st.routeHi = null;   // a route was found over the window it was shown in
    st.win = [Math.min(drag.m, m), Math.max(drag.m, m)];
    if (st.win[1] - st.win[0] < 1) st.win[1] = st.win[0] + 1;
    schedule();
  });
  hc.addEventListener('pointerup', () => {
    if (drag && !drag.moved && st.win) { st.win = null; st.newOnly = false; st.routeHi = null; schedule(); }
    drag = null;
  });
}

/* ================================================ hover, clicks and menus */

function wireStage() {
  const stage = D.stage;
  const at = (ev) => {
    const r = stage.getBoundingClientRect();
    const vy = ev.clientY - r.top;
    return hit ? hit.at(ev.clientX - r.left, vy + stage.scrollTop, vy) : null;
  };
  stage.addEventListener('mousemove', (ev) => {
    if (!hit) return;
    const got = at(ev);
    const prevAcct = st.hoverAcct, prevHost = st.hoverHost, prevCur = hit.cur;
    st.hoverAcct = !got ? null : got.link && hit.kind === 'chain' ? got.link.u : got.edge ? got.edge.h.u
      : got.sw ? got.sw.to : got.sess ? got.sess.u : null;
    st.hoverHost = got && got.host != null ? got.host : null;
    hit.cur = got;
    stage.style.cursor = got && (got.host != null || got.sw || got.link) ? 'pointer' : 'default';
    const wr = D.wrap.getBoundingClientRect();
    const tx = ev.clientX - wr.left, ty = ev.clientY - wr.top;
    if (got && got.sw) showTip(switchTip(got.sw), tx, ty);
    else if (got && got.sess) showTip(sessTip(got.sess), tx, ty);
    else if (got && got.link) showTip(linkTip(got.link), tx, ty);
    else if (got && got.edge) {
      const h = got.edge.h, x = got.edge.sw;
      const sw = !x ? '' : x.how === 'explicit' ? `<div class="swt">${esc(shortAcct(x.from))} used these credentials here</div>`
        : `<div class="swt">Credential switch: in as ${esc(shortAcct(x.from))}, out as ${esc(shortAcct(x.to))}</div>`;
      showTip(`<div class="t">${esc(HOSTS[h.s])} → ${esc(HOSTS[h.d])}</div><div class="d">${esc(acctName(h.u))}</div>`
        + `<div class="k"><i style="background:${evColor(h.k)}"></i>${esc(EV[h.k].short)}</div><div class="d">${fmt(h.t)}</div>${sw}`, tx, ty);
    } else if (got && got.host != null) showTip(hostTip(got.host), tx, ty);
    else hideTip();
    // what is under the pointer is rebuilt with every redraw: compare what it is, not the object
    const same = (a, b) => (!a || !b ? a === b : a.host === b.host && a.sw === b.sw && a.sess === b.sess
      && (a.link && b.link ? linkKey(a.link) === linkKey(b.link) : a.link === b.link)
      && (a.edge && b.edge ? a.edge.h === b.edge.h : a.edge === b.edge));
    if (prevAcct !== st.hoverAcct || prevHost !== st.hoverHost || !same(prevCur, got)) redrawStage();
  });
  stage.addEventListener('mouseleave', () => {
    hideTip();
    if (hit) hit.cur = null;
    if (st.hoverAcct != null || st.hoverHost != null) { st.hoverAcct = null; st.hoverHost = null; redrawStage(); }
  });
  stage.addEventListener('scroll', () => { hideTip(); if (hit) scheduleRedraw(); }, { passive: true });
  stage.addEventListener('contextmenu', (ev) => {
    const got = at(ev);
    if (!got) return;
    ev.preventDefault();
    if (got.host != null) openMenu({ host: got.host }, ev);
    else if (got.sw) openMenu({ sw: got.sw }, ev);
    else if (got.sess) openMenu({ link: { s: got.sess.s, d: got.sess.d, u: got.sess.u, sels: new Map([[got.sess.sel, 1]]) } }, ev);
    else if (got.link) openMenu({ link: got.link }, ev);
    else if (got.edge) openMenu({ link: { s: got.edge.h.s, d: got.edge.h.d, u: got.edge.h.u, sels: new Map([[got.edge.h.sel, got.edge.h.n]]) } }, ev);
  });
  stage.addEventListener('click', (ev) => {
    const got = at(ev);
    if (!got) return;
    if (got.host != null) openMenu({ host: got.host }, ev);
    else if (got.sw) openMenu({ sw: got.sw }, ev);
    else if (got.link && hit.kind === 'chain') openMenu({ link: got.link }, ev);
  });
}

/* The app's own menu (winnow.dropdownMenu) at the pointer: it anchors
   under an element, so the element is a point where the click was. */
function menuAt(ev, items) {
  if (D.anchor) D.anchor.remove();
  const a = ctx.el('span');
  a.style.cssText = `position:fixed;left:${ev.clientX}px;top:${ev.clientY}px;width:0;height:0`;
  document.body.append(a);
  D.anchor = a;
  ctx.dropdownMenu(a, items);
}

// The event's own conditions, in the filter builder's spelling: "is none
// of" has no single operator there, so it is one "is not" per value.
function whereOf(def) {
  const out = [];
  for (const c of def.conditions || []) {
    if (c.op === 'in') out.push({ column: c.column, op: 'in', value: listOf(c.value) });
    else if (c.op === 'not_in') for (const v of listOf(c.value)) out.push({ column: c.column, op: 'not_equals', value: v });
    else out.push({ column: c.column, op: c.op, value: String(c.value ?? '') });
  }
  return out;
}
const spellingsOf = (h, sel, role) => (SPELL.get(h) || []).filter((x) => x.sel === sel && x.role === role).map((x) => x.raw);
function openRows(sel, where) {
  const s = SELS[sel];
  if (!s) return;
  ctx.openFiltered(s.source_id, [], { where: [...where, ...whereOf(s.def)] });
}
// "Open its rows": the table and column a host is written in, with every
// spelling the feed saw for it there and the event's own conditions.
function hostRowItems(h) {
  const groups = new Map();
  for (const x of SPELL.get(h) || []) {
    const key = `${x.sel}|${x.role}`;
    if (!groups.has(key)) groups.set(key, { sel: x.sel, role: x.role, raws: [], n: 0 });
    const gr = groups.get(key);
    gr.raws.push(x.raw); gr.n += x.n;
  }
  return [...groups.values()].sort((a, b) => b.n - a.n).slice(0, 4).filter((gr) => SELS[gr.sel]).map((gr) => {
    const s = SELS[gr.sel], col = gr.role ? s.def.dst_col : s.def.src_col;
    return { label: `Open its rows in ${s.src.name}`, desc: `${col} as ${gr.role ? 'destination' : 'source'}, ${shortName(s.def.name)} · ${plural(gr.n, 'row')}`,
      onclick: () => openRows(gr.sel, [{ column: col, op: 'in', value: gr.raws }]) };
  });
}
function linkRowItems(L) {
  return [...L.sels.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3).filter(([sel]) => SELS[sel]).flatMap(([sel, n]) => {
    const s = SELS[sel], src = spellingsOf(L.s, sel, 0), dst = spellingsOf(L.d, sel, 1);
    if (!src.length || !dst.length) return [];
    const where = [{ column: s.def.src_col, op: 'in', value: src }, { column: s.def.dst_col, op: 'in', value: dst }];
    const single = L.u != null && (!L.users || L.users.size === 1);
    if (single && L.u !== NOACCT && s.def.label_col) {
      const raws = (USPELL.get(L.u) || []).filter((x) => x.sel === sel).map((x) => x.raw);
      if (raws.length) where.push({ column: s.def.label_col, op: 'in', value: raws });
    }
    return [{ label: `Open the rows behind it${L.sels.size > 1 ? ` in ${s.src.name}` : ''}`,
      desc: `${shortName(s.def.name)} · ${plural(n, 'row')}`, onclick: () => openRows(sel, where) }];
  });
}
function accountRowItems(u) {
  const bySel = new Map();
  for (const x of USPELL.get(u) || []) {
    if (!bySel.has(x.sel)) bySel.set(x.sel, { raws: [], n: 0 });
    const gr = bySel.get(x.sel);
    gr.raws.push(x.raw); gr.n += x.n;
  }
  return [...bySel.entries()].sort((a, b) => b[1].n - a[1].n).slice(0, 3).filter(([sel]) => SELS[sel] && SELS[sel].def.label_col)
    .map(([sel, gr]) => {
      const s = SELS[sel];
      return { label: `Open its rows in ${s.src.name}`, desc: `${s.def.label_col}, ${shortName(s.def.name)} · ${plural(gr.n, 'row')}`,
        onclick: () => openRows(sel, [{ column: s.def.label_col, op: 'in', value: gr.raws }]) };
    });
}

function openMenu(what, ev) {
  hideTip();
  const items = [];
  const go = (fn) => () => { fn(); render(); };
  const openPath = (p) => { st.path = p; st.pathFrom = null; st.routeHi = null; st.mode = 'path'; };
  const hostItems = (h) => {
    items.push({ header: HOSTS[h] });
    if (st.pathFrom != null && st.pathFrom !== h) {
      const a = st.pathFrom;
      items.push({ label: `Path from ${HOSTS[a]} to here`, desc: 'Time-ordered routes, explained steps first', onclick: go(() => openPath({ from: a, to: h })) });
    }
    items.push({ label: `Pivot from ${HOSTS[h]}`, desc: 'Where its access came from, and where it went next', onclick: go(() => { st.pivot = h; st.mode = 'pivot'; }) });
    const jewels = [...st.jewels].filter((j) => j !== h);
    if (jewels.length) {
      items.push({ label: 'Paths from here to crown jewels', desc: jewels.map((j) => HOSTS[j]).join(', '), onclick: go(() => openPath({ from: h, jewels: true })) });
    }
    items.push(st.pathFrom === h
      ? { label: 'Cancel the path from here', onclick: go(() => { st.pathFrom = null; }) }
      : { label: 'Find a path from here…', desc: 'Then right-click the host it should end at', onclick: go(() => { st.pathFrom = h; }) });
    if (st.jewels.has(h)) items.push({ label: 'Unmark as crown jewel', onclick: go(() => { st.jewels.delete(h); saveState(); }) });
    else {
      // Defender for Identity treats domain controllers as sensitive out of
      // the box; here the hint only says what the case shows, since
      // everyone's morning logon reaches a DC.
      const machines = USERS.filter((_, u) => MACHINE[u]).length;
      const into = new Set(HOPS.filter((x) => x.d === h && MACHINE[x.u]).map((x) => x.u)).size;
      items.push({ label: 'Mark as crown jewel', desc: machines >= 4 && into >= machines / 2
        ? 'Most machine accounts log on here: probably a domain controller' : 'Routes that end here get a star, and a menu item of their own',
      onclick: go(() => { st.jewels.add(h); saveState(); }) });
    }
    items.push({ label: st.host === h ? 'Show every host again' : 'Only hops that touch this host', onclick: go(() => { st.host = st.host === h ? null : h; }) });
    const rows = hostRowItems(h);
    if (rows.length) items.push('-', ...rows);
  };
  // Hopper drops service accounts and bastion traffic before it looks for
  // anything; here the analyst decides, and the hint says what the case shows.
  const expectedItem = (u) => {
    if (st.expected.has(u)) { items.push({ label: `Stop hiding ${shortAcct(u)}`, onclick: go(() => { st.expected.delete(u); saveState(); }) }); return; }
    const perDay = new Map();
    for (const h of HOPS) if (h.u === u) { const d = dayOf(h.t); if (!perDay.has(d)) perDay.set(d, new Set()); perDay.get(d).add(h.d); }
    const sets = [...perDay.values()].map((s) => [...s].sort().join());
    const habit = caseDays() > 1 && perDay.size === caseDays() && new Set(sets).size === 1;
    const n = habit ? [...perDay.values()][0].size : 0;
    items.push({ label: `Mark ${shortAcct(u)} as expected`,
      desc: habit ? `Reaches the same ${plural(n, 'host')} every day of the case. Hides it everywhere.` : 'Hides it everywhere, like a known service account',
      onclick: go(() => { st.expected.add(u); if (st.account === u) st.account = null; saveState(); }) });
  };
  if (what.host != null) hostItems(what.host);
  if (what.sw) {
    const x = what.sw;
    items.push({ header: x.how === 'explicit' ? `Explicit credentials on ${HOSTS[x.host]}` : `Credential switch on ${HOSTS[x.host]}` });
    items.push({ label: `Pivot from ${HOSTS[x.host]}`, onclick: go(() => { st.pivot = x.host; st.mode = 'pivot'; }) });
    items.push({ label: `Only ${USERS[x.to]}`, onclick: go(() => { st.account = x.to; }) });
    items.push({ label: `Only ${USERS[x.from]}`, onclick: go(() => { st.account = x.from; }) });
  }
  if (what.link) {
    const L = what.link;
    items.push({ header: `${HOSTS[L.s]} → ${HOSTS[L.d]}` });
    items.push({ label: `Pivot from ${HOSTS[L.s]}`, onclick: go(() => { st.pivot = L.s; st.mode = 'pivot'; }) });
    items.push({ label: `Pivot from ${HOSTS[L.d]}`, onclick: go(() => { st.pivot = L.d; st.mode = 'pivot'; }) });
    const u = L.u != null && (!L.users || L.users.size === 1) ? L.u : null;
    if (u != null) {
      items.push({ label: st.account === u ? 'Show every account again' : u === NOACCT ? 'Only hops with no account' : `Only ${USERS[u]}`,
        onclick: go(() => { st.account = st.account === u ? null : u; }) });
    }
    if (u != null && u !== NOACCT) expectedItem(u);
    const rows = L.sels ? linkRowItems(L) : [];
    if (rows.length) items.push('-', ...rows);
  }
  if (what.account != null) {
    const u = what.account;
    items.push({ header: USERS[u] });
    items.push({ label: 'Show its hops in Chain', onclick: go(() => { st.account = u; st.mode = 'chain'; }) });
    items.push({ label: 'Show its hops in Host matrix', onclick: go(() => { st.account = u; st.mode = 'matrix'; }) });
    expectedItem(u);
    const rows = accountRowItems(u);
    if (rows.length) items.push('-', ...rows);
  }
  if (items.length) menuAt(ev, items);
}

/* ================================================================ render */

let raf = 0, rafRedraw = 0;
function schedule() {
  if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); });
}
function scheduleRedraw() {
  if (!rafRedraw) rafRedraw = requestAnimationFrame(() => { rafRedraw = 0; redrawStage(); });
}
// A hover or a scroll repaints the canvas view on screen, and nothing else.
function redrawStage() {
  if (!F || !D || !D.root.isConnected) return;
  readPalette();
  if (st.mode === 'chain') drawChain(F);
  else if (st.mode === 'matrix') drawMatrix(F);
  else if (st.mode === 'pivot') drawPivot(F);
}

function render() {
  if (!D || !D.root.isConnected || D.stage.clientWidth === 0) return;
  hideTip();
  readPalette();
  ON = new Set(EV.filter((e) => !st.off.has(e.name)).map((e) => e.k));
  if ((st.mode === 'pivot' && st.pivot == null) || (st.mode === 'path' && st.path == null)) st.mode = 'chain';
  paintModes();
  paintFilters();
  if (S.pendingNote && !S.busy) noteAfterSave();
  paintHead();
  const status = (t) => paintStatus(t);
  if (!HOPS.length) {
    F = null; hit = null;
    D.hist.hidden = true;
    emptyStage(...emptyMessage());
    status(S.busy ? 'Reading the movement events…' : '');
    return;
  }
  D.hist.hidden = false;
  const key = computeKey();
  if (!F || F.key !== key) { F = compute(); F.key = key; }
  const keep = D.stage.scrollTop;
  if (hit) hit.cur = null;
  st.hoverAcct = null; st.hoverHost = null;
  if (st.mode === 'chain') drawChain(F);
  else if (st.mode === 'matrix') drawMatrix(F);
  else if (st.mode === 'accounts') drawAccounts(F);
  else if (st.mode === 'pivot') drawPivot(F);
  else drawPath(F);
  if (st.mode === 'accounts' || st.mode === 'path') D.stage.scrollTop = 0;
  else if (D.stage.scrollTop !== keep) D.stage.scrollTop = keep;
  drawHist(F);
  const s = F.shown || {};
  const rows = F.hops.reduce((a, h) => a + h.n, 0);
  let text = st.mode === 'accounts' ? `${plural(s.accounts || 0, 'account')} · ${plural(rows, 'row')}`
    : st.mode === 'pivot' ? `${plural(s.hosts || 0, 'host')} within three hops of ${HOSTS[st.pivot]}`
      : st.mode === 'path' ? (s.best ? `${plural(s.routes, 'route')} · best: ${plural(s.best.hops.length, 'hop')}, `
        + `${s.best.score[0] ? `${s.best.score[0]} unexplained` : s.best.score[1] ? `${s.best.score[1]} with no account` : 'every step explained'}` : 'no route')
        : `${plural(s.hosts || 0, 'host')} · ${plural(s.links || 0, 'link')} · ${plural(rows, 'row')}`
          + (st.mode === 'chain' && s.switches ? ` · ${s.switches} credential switch${s.switches === 1 ? '' : 'es'}` : '');
  if (s.cut) text += ` · showing ${s.cut}`;
  if (S.timeApplied) text += ' · timeframe applied';
  if (TRUNC) text += ' · the earliest hops only: narrow the timeframe for the rest';
  status(S.busy ? 'Reading the movement events…' : text);
}

function emptyMessage() {
  const { el } = ctx;
  if (!liveSources().length) return ['Import a table first: the views draw the hops its movement events record.'];
  if (S.error) return [`Could not read the movement events: ${S.error}`];
  if (S.busy || !S.started) return ['Reading the movement events…'];
  const offered = liveSources().some((src) => eventsFor(src).length);
  if (!offered) {
    const b = el('button', 'btn', '+ New event type');
    b.type = 'button';
    b.onclick = () => openEventBuilder(ctx, { onSave: addEvent });
    return ['No table here has the columns any movement event reads. Define one for the columns you have.', b];
  }
  if (!resolvedSelections().length) return ['Tick a movement event above to draw the hops it records.'];
  const per = PER.reduce((a, p) => ({ rows: a.rows + p.rows, untimed: a.untimed + p.untimed }), { rows: 0, untimed: 0 });
  if (per.rows && per.untimed === per.rows) return ['The ticked events match rows, but none has a time the views can place. Check each event\'s Time column in the builder.'];
  return [S.timeApplied ? 'The ticked events draw no hops inside the case timeframe.' : 'The ticked events draw no hops in these tables.'];
}

/* ======================================================= movement events */

function renderPanel() {
  if (!D) return;
  const { el } = ctx;
  D.panel.replaceChildren();
  const sources = liveSources();
  if (!sources.length) { D.panel.append(el('div', 'lm-dim', 'Import a table first.')); return; }
  const bare = [];
  for (const src of sources) {
    const events = eventsFor(src);
    if (!events.length) { bare.push(src); continue; }
    D.panel.append(el('div', 'lm-src-head', `${src.name} · ${src.row_count.toLocaleString()} rows`));
    for (const d of events) {
      const key = selKey(src.id, d.name);
      const on = S.selections.some((s) => s.key === key);
      const row = el('label', 'lm-event');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = on;
      cb.onchange = () => {
        if (cb.checked) S.selections.push({ key, source_id: src.id, name: d.name });
        else S.selections = S.selections.filter((s) => s.key !== key);
        S.note = null;
        selectionsChanged();
      };
      const sw = el('span', 'lm-swatch');
      const e = EV.find((x) => x.name === d.name);
      if (on && e) sw.style.background = `var(--series-${e.slot})`;
      row.append(cb, sw, el('span', 'lm-event-name', d.name));
      if (S.defs.saved.includes(d)) row.append(el('span', 'lm-yours', 'yours'));
      row.append(el('span', 'lm-event-desc', d.description || ''));
      const i = SELS.findIndex((s) => s.key === key);
      if (on && i >= 0 && PER[i]) {
        const p = PER[i], n = el('span', 'lm-event-n', plural(p.drawn, 'row'));
        const left = p.rows - p.drawn;
        n.title = `${plural(p.drawn, 'row')} draw a hop`
          + (left ? `; ${left.toLocaleString()} more name no host, the same host twice, or no time` : '');
        row.append(n);
      }
      D.panel.append(row);
    }
  }
  if (bare.length) {
    const line = el('div', 'lm-bare');
    line.append(`Nothing reads ${bare.length === 1 ? '' : `${bare.length} tables, `}`);
    bare.slice(0, 6).forEach((src, i) => {
      if (i) line.append(', ');
      const b = el('button', 'lm-link', src.name);
      b.type = 'button';
      b.title = `Define a movement event that reads ${src.name}`;
      b.onclick = () => openEventBuilder(ctx, { sourceId: src.id, onSave: addEvent });
      line.append(b);
    });
    if (bare.length > 6) line.append(` and ${bare.length - 6} more`);
    line.append('. Click one to define a movement event for its columns.');
    D.panel.append(line);
  }
}

function selectionsChanged() {
  renderPanel();
  paintHead();
  saveState();
  scheduleFetch();
}

// After a save: what the new event adds, once the feed has it.
function noteAfterSave() {
  const { name, edit } = S.pendingNote;
  S.pendingNote = null;
  const e = EV.find((x) => x.name === name);
  if (!e) { S.note = `${edit ? 'Saved' : 'Added'} "${name}"`; return; }
  const rows = e.sels.reduce((a, i) => a + (PER[i] ? PER[i].drawn : 0), 0);
  const F0 = compute();
  const own = F0.base.filter((h) => h.k === e.k).reduce((a, h) => a + h.n, 0);
  S.note = `${edit ? 'Saved' : 'Added'} "${name}": ${plural(rows, 'row')}`
    + (own < rows ? `, ${own === 1 ? '1 of them a hop' : `${own.toLocaleString()} of them hops`} no other event has` : ', drawn in every view');
}

/* Whether a definition is offered on a table. Tied to a header set, it
   binds only on a table that IS that artifact (has every column the set
   defines), the way a filter default binds; a set that isn't known
   (older defs) falls back to the explicit column list. Either way the
   table must also carry every column the event itself reads: a
   condition on a column the table lacks used to bind anyway and fail
   when the views asked for it with "No column". */
function bindsTo(d, src) {
  const cols = new Set(src.columns.map((c) => c.name));
  const hs = d.header_set && S.defs.header_sets && S.defs.header_sets[d.header_set];
  if (hs && !hs.every((c) => cols.has(c))) return false;
  return [...(d.requires || []), ...requiresOf(d)].every((c) => cols.has(c));
}

// Every column an event reads, in the order the builder shows them.
function requiresOf(d) {
  return [...new Set([d.src_col, d.dst_col, d.label_col, d.actor_col, d.time_col,
    ...(d.conditions || []).map((c) => c.column), d.session && d.session.column].filter(Boolean))];
}

/* Ticks follow the definitions they name: an edited event keeps its
   ticks where it still binds, a deleted one loses them. */
function syncSelections() {
  const byName = new Map(allDefs().map((d) => [d.name, d]));
  const sources = new Map(liveSources().map((s) => [s.id, s]));
  S.selections = S.selections.filter((s) => {
    const d = byName.get(s.name), src = sources.get(s.source_id);
    return d && src && bindsTo(d, src);
  });
}

// A freshly defined event is usually the one you want next: tick it
// wherever it binds.
async function addEvent(def) {
  S.defs.saved.push(def);
  await saveDefs(ctx);
  for (const src of liveSources()) {
    if (bindsTo(def, src) && !S.selections.some((sl) => sl.key === selKey(src.id, def.name))) {
      S.selections.push({ key: selKey(src.id, def.name), source_id: src.id, name: def.name });
    }
  }
  S.pendingNote = { name: def.name, edit: false };
  syncSelections();
  selectionsChanged();
}

async function replaceEvent(oldName, def) {
  const i = S.defs.saved.findIndex((d) => d.name === oldName);
  if (i < 0) return addEvent(def);
  S.defs.saved[i] = def;
  // a rename carries its ticks, and its hidden state, across to the new name
  for (const s of S.selections) {
    if (s.name === oldName) { s.name = def.name; s.key = selKey(s.source_id, def.name); }
  }
  if (st.off.delete(oldName)) st.off.add(def.name);
  await saveDefs(ctx);
  S.pendingNote = { name: def.name, edit: true };
  syncSelections();
  selectionsChanged();
  return undefined;
}

async function deleteEvent(d) {
  S.defs.saved = S.defs.saved.filter((x) => x !== d);
  await saveDefs(ctx);
  S.note = null;
  syncSelections();
  selectionsChanged();
}

async function loadDefs() {
  try { S.defs = await ctx.api(`${ctx.base}/defs`); }
  catch { S.defs = { shipped: [], saved: [], header_sets: {} }; }
}

async function saveDefs(winnow) {
  try { S.defs = await winnow.post(`${winnow.base}/defs`, { saved: S.defs.saved }); }
  catch (e) { winnow.toast('Could not save: ' + e.message, 6000); }
}

/* The Events… modal, in the app's two-pane frame (winnow.sectionPanes):
   yours and the shipped ones down a side list, each list on the right
   with the hop it draws, where it binds and how many of this case's
   tables it binds to. What a row offers is in its ⋯; its name opens it
   in the builder. */
function openEventManager(winnow, section = null) {
  const { el, modal, confirmDialog, dropdownMenu, sectionPanes } = winnow;
  const reopen = (at) => openEventManager(winnow, at);
  const bound = (d) => liveSources().filter((s) => bindsTo(d, s)).length;
  modal('Movement events', (b) => {
    const edit = (d) => openEventBuilder(winnow, { edit: d, onSave: (def) => replaceEvent(d.name, def), onDone: () => reopen('saved') });
    const copy = (d) => openEventBuilder(winnow, { template: d, onSave: addEvent, onDone: () => reopen('saved') });
    const del = async (d) => {
      if (!(await confirmDialog(`Delete the movement event "${d.name}"?`, { okLabel: 'Delete', danger: true }))) return;
      await deleteEvent(d);
      reopen('saved');
    };
    const menu = (d, kind) => (kind === 'shipped'
      ? [{ label: 'Copy to a new event…', onclick: () => copy(d) }]
      : [{ label: 'Edit…', onclick: () => edit(d) }, { label: 'Copy to a new event…', onclick: () => copy(d) }, '-',
        { label: 'Delete…', onclick: () => del(d) }]);

    const buildList = (kind) => (pane) => {
      const defs = kind === 'shipped' ? S.defs.shipped : S.defs.saved;
      pane.append(el('p', 'lm-mgr-note', kind === 'shipped'
        ? 'These come with the plugin. Copy one to start an event of your own from it.'
        : 'Kept on this machine and offered in every case.'));
      const box = el('div', 'lm-mgr-box');
      const head = el('div', 'lm-mgr-row lm-mgr-headrow');
      head.append(el('span', null, 'Event'), el('span', null, 'Hop'), el('span', null, 'Binds to'), el('span', 'lm-mgr-num', 'Tables'), el('span'));
      const list = el('div', 'lm-mgr-list');
      for (const d of defs) {
        const r = el('div', 'lm-mgr-row');
        const name = el('button', 'lm-mgr-name', d.name);
        name.type = 'button';
        name.title = kind === 'shipped' ? 'Copy to a new event' : 'Edit this event';
        name.onclick = () => (kind === 'shipped' ? copy(d) : edit(d));
        const n = bound(d);
        const more = el('button', 'btn ghost lm-mgr-more', '⋯');
        more.type = 'button';
        more.setAttribute('aria-label', `Actions for ${d.name}`);
        more.setAttribute('aria-haspopup', 'menu');
        more.onclick = () => dropdownMenu(more, menu(d, kind));
        r.append(name, el('span', 'lm-mgr-sub', `${d.src_col} → ${d.dst_col}`), el('span', 'lm-mgr-sub', bindingText(d)),
          el('span', 'lm-mgr-num' + (n ? ' some' : ''), n ? String(n) : '—'), more);
        list.append(r);
      }
      if (!defs.length) {
        list.append(el('p', 'lm-mgr-empty', kind === 'shipped' ? 'The plugin ships no events.'
          : 'None yet. + New event… below starts one, or copy a shipped event from its ⋯.'));
      }
      box.append(head, list);
      pane.append(box);
    };
    const frame = sectionPanes({
      sections: [
        { id: 'saved', group: 'Events', title: 'Yours', build: buildList('saved'),
          hint: () => (S.defs.saved.length ? plural(S.defs.saved.length, 'event') : 'None yet') },
        { id: 'shipped', group: 'Events', title: 'Shipped', build: buildList('shipped'),
          hint: () => `${plural(S.defs.shipped.length, 'event')} · ${S.defs.shipped.filter((d) => bound(d)).length} bind here` },
      ],
      label: 'Movement event lists', cls: 'lm-mgr-panes', paneCls: 'lm-mgr-pane',
    });
    b.append(frame.panes);
    const foot = el('div', 'lm-mgr-foot');
    const add = el('button', 'btn primary', '+ New event…');
    add.onclick = () => openEventBuilder(winnow, { onSave: addEvent, onDone: () => reopen('saved') });
    foot.append(add);
    b.append(foot);
    frame.show(section || (S.defs.saved.length ? 'saved' : 'shipped'));
  }, { wide: true, tall: true });
}

function bindingText(d) {
  return d.header_set ? `${d.header_set} tables` : 'any table with its columns';
}

/* ============================================================ event builder

   One dialog for a new event, a copy of a shipped one, or an edit of one
   of yours. It has the merge builder's shape: the definition on the
   left, read against one real table in the case, and on the right what
   that definition finds there (rows matched, links, hosts, the busiest
   links), refreshed as it changes. A wrong column or a condition value
   spelled the way the export doesn't spell it shows up here, before the
   event is saved, instead of as an empty view afterwards. */

const OP_LABELS = [
  ['equals', 'is'], ['not_equals', 'is not'], ['in', 'is one of'],
  ['not_in', 'is none of'], ['contains', 'contains'], ['not_contains', 'does not contain'],
];
const LIST_OPS = new Set(['in', 'not_in']);

/* Column guesses, per role and per table: the first pattern that matches
   any column wins, so an exact name (UserName) beats a loose one (the
   SID in UserId). The acting account is never guessed: only a row that
   names two accounts has one, and a wrong guess would invent switches. */
const GUESS = {
  src: [/^(remote|source|src|client|calling)[ _-]?(host|hostname|ip|ip ?address|addr(ess)?|name|workstation|computer)?$/i,
    /workstation|ip ?address|remote|source|src|client|caller/i],
  dst: [/^(dest(ination)?|dst|target)[ _-]?(host|hostname|ip|ip ?address|addr(ess)?|name|computer|server)?$/i,
    /^computer(name)?$/i, /dest|dst|target|server|computer|host/i],
  label: [/^(target)?user[ _-]?name$/i, /^(user|account|subject|principal)$/i, /user|account|principal|subject/i],
  time: [/^(time ?created|timestamp|time ?generated|event ?time|date ?time)$/i, /time|date|created|when|utc/i],
};
const ID_LIKE = /(Id|ID|Sid|SID|[_\s-]id)$/;

function guessColumn(src, role, avoid) {
  let cols = src.columns.filter((c) => !avoid.includes(c.name));
  if (role === 'label') cols = cols.filter((c) => !ID_LIKE.test(c.name));
  const tiers = role === 'time' ? [cols.filter((c) => c.type === 'datetime'), cols] : [cols];
  for (const re of GUESS[role]) {
    for (const tier of tiers) {
      const found = tier.find((c) => re.test(c.name));
      if (found) return found.name;
    }
  }
  if (role === 'time') { const dt = cols.find((c) => c.type === 'datetime'); if (dt) return dt.name; }
  return '';
}

function headerSetsOf(src) {
  const cols = new Set(src.columns.map((c) => c.name));
  return Object.entries(S.defs.header_sets || {})
    .filter(([, hs]) => hs.every((c) => cols.has(c)))
    .sort((a, b) => b[1].length - a[1].length)
    .map(([name]) => name);
}

function openEventBuilder(winnow, { edit = null, template = null, sourceId = null, onSave, onDone = null } = {}) {
  const { el, modal, post, closeModal } = winnow;
  const sources = liveSources();
  const taken = new Map([...S.defs.shipped.map((d) => [d.name.toLowerCase(), 'a shipped event']),
    ...S.defs.saved.filter((d) => d !== edit).map((d) => [d.name.toLowerCase(), 'one of your events'])]);
  const ROLES = ['src', 'dst', 'label', 'actor', 'time'];
  const b = {
    sourceId, binding: '', bindingTouched: false,
    name: '', nameTouched: false, base: null, description: '',
    src_col: '', dst_col: '', label_col: '', actor_col: '', time_col: '', strip_prefix: false, fails: false,
    session: null, keepSession: true, conditions: [],
    // Roles the definition or the analyst chose, as opposed to guessed:
    // a table switch re-guesses only the rest, so previewing an edited
    // event against another table never quietly rewrites it.
    picked: new Set(),
  };
  const srcOf = () => sources.find((s) => s.id === b.sourceId) || null;
  const hasCol = (name) => !!srcOf() && srcOf().columns.some((c) => c.name === name);

  // Copy a definition into the builder and read it against a table it
  // binds to, when the case has one.
  function load(def) {
    for (const role of ROLES) {
      b[`${role}_col`] = def[`${role}_col`] || '';
      if (b[`${role}_col`]) b.picked.add(role);
    }
    b.strip_prefix = !!def.strip_prefix;
    b.fails = !!def.fails;
    b.session = def.session || null;
    b.keepSession = true;
    b.conditions = (def.conditions || []).map((c) => ({ column: c.column, op: c.op, value: c.value ?? '' }));
    b.description = def.description || '';
    b.binding = def.header_set || '';
    b.bindingTouched = true;
    const home = sources.find((s) => bindsTo(def, s));
    if (home && sourceId == null) b.sourceId = home.id;
  }
  // Guess every role nobody chose from the table's own column names.
  function guessMissing() {
    const src = srcOf();
    if (!src) return;
    for (const role of ['src', 'dst', 'label', 'time']) {
      if (b.picked.has(role)) continue;
      const avoid = ['src', 'dst'].filter((r) => r !== role && b.picked.has(r)).map((r) => b[`${r}_col`]);
      if (role === 'dst' && b.src_col) avoid.push(b.src_col);
      b[`${role}_col`] = guessColumn(src, role, avoid);
    }
    if (!b.picked.has('actor')) b.actor_col = '';
    if (!b.bindingTouched) b.binding = headerSetsOf(src)[0] || '';
  }

  if (edit) { load(edit); b.name = edit.name; b.nameTouched = true; }
  else if (template) { load(template); b.base = template.name; }
  if (b.sourceId == null) {
    // The open table when it has something that reads as a hop, else
    // the first table that does.
    const hoppy = sources.filter((s) => guessColumn(s, 'src', []) && guessColumn(s, 'dst', [guessColumn(s, 'src', [])]));
    const cur = (id) => id === winnow.state.sourceId;
    b.sourceId = (hoppy.find((s) => cur(s.id)) || hoppy[0] || sources.find((s) => cur(s.id)) || sources[0] || {}).id ?? null;
  }
  if (!edit && !template) guessMissing();

  // A name that follows the definition until the analyst types one —
  // "EventId 5985: RemoteHost → Computer" — never one that's taken.
  function suggestedName() {
    let base = b.base ? `${b.base} (copy)` : '';
    if (!base) {
      const conds = b.conditions.filter((c) => c.column && String(c.value).trim()
        && ['equals', 'in'].includes(c.op)).map((c) => `${c.column} ${String(c.value).trim()}`);
      const hop = b.src_col && b.dst_col ? `${b.src_col} → ${b.dst_col}` : 'New movement event';
      base = conds.length ? `${conds.join(', ')}: ${hop}` : hop;
    }
    let name = base;
    for (let n = 2; taken.has(name.toLowerCase()); n++) name = `${base} (${n})`;
    return name;
  }
  const currentName = () => (b.nameTouched ? b.name : suggestedName()).trim();

  // A condition counts once it has a column and a value. A row just
  // added has neither, and previewing "EventId is (blank)" would only
  // ever report that nothing matches.
  const hasValue = (c) => (LIST_OPS.has(c.op) ? listOf(c.value) : [String(c.value ?? '').trim()]).some(Boolean);
  const usableConditions = () => b.conditions.filter((c) => c.column && hasValue(c));
  const waitingConditions = () => b.conditions.filter((c) => c.column && !hasValue(c));
  function problem() {
    if (!b.src_col || !b.dst_col) return 'Pick a source and a destination column.';
    if (b.src_col === b.dst_col) return 'Source and destination need to be different columns.';
    if (b.actor_col && b.actor_col === b.label_col) return 'The acting account needs a column of its own, not the user column.';
    const half = waitingConditions()[0];
    if (half) return `The ${half.column} condition needs a value, or remove it.`;
    if (b.conditions.some((c) => !c.column && hasValue(c))) return 'Pick a column for every condition, or remove it.';
    const name = currentName();
    if (!name) return 'Give the event a name.';
    if (taken.has(name.toLowerCase())) return `"${name}" is already the name of ${taken.get(name.toLowerCase())}.`;
    return '';
  }

  let tableSel, segBox, segNote, stripCb, failCb, sessBox, condBox, nameInput, descInput, saveBtn, hint;
  let statsBox, notesBox, linksBox, previewHead;
  const roleSels = {};
  // Declared before modal() runs the builder, which already uses them.
  let timer = 0, seq = 0;
  const valueCache = new Map();

  modal(edit ? `Edit movement event — ${edit.name}` : 'New movement event', (box) => {
    if (!sources.length) {
      box.append(el('p', null, 'Import a table first. The builder reads its columns from one, and shows what the event finds there.'));
      return;
    }
    const root = el('div', 'lm-eb');
    const panes = el('div', 'lm-eb-panes');
    const left = el('div', 'lm-eb-pane');

    // ---- start from (a new event only)
    if (!edit) {
      const startSel = el('select');
      startSel.setAttribute('aria-label', 'Start from');
      startSel.append(new Option('A blank event', ''));
      for (const [label, defs] of [['Shipped', S.defs.shipped], ['Yours', S.defs.saved]]) {
        if (!defs.length) continue;
        const g = document.createElement('optgroup');
        g.label = label;
        defs.forEach((d, i) => g.append(new Option(d.name, `${label}:${i}`)));
        startSel.append(g);
      }
      if (template) {
        const i = S.defs.shipped.indexOf(template), j = S.defs.saved.indexOf(template);
        startSel.value = i >= 0 ? `Shipped:${i}` : (j >= 0 ? `Yours:${j}` : '');
      }
      startSel.onchange = () => {
        const [kind, i] = startSel.value.split(':');
        const def = kind ? (kind === 'Shipped' ? S.defs.shipped : S.defs.saved)[+i] : null;
        Object.assign(b, { src_col: '', dst_col: '', label_col: '', actor_col: '', time_col: '', strip_prefix: false, fails: false,
          session: null, conditions: [], description: '', base: null, bindingTouched: false, picked: new Set() });
        if (def) { load(def); b.base = def.name; }
        guessMissing();
        refresh();
      };
      left.append(section('Start from', startSel));
    }

    // ---- which table the columns come from, and where the event is offered
    tableSel = el('select');
    tableSel.setAttribute('aria-label', 'Read columns from');
    for (const s of sources) tableSel.append(new Option(`${s.name} · ${s.row_count.toLocaleString()} rows`, String(s.id)));
    tableSel.onchange = () => { b.sourceId = +tableSel.value; guessMissing(); refresh(); };
    segBox = el('div', 'lm-eb-seg');
    segBox.setAttribute('role', 'group');
    segBox.setAttribute('aria-label', 'Offer it on');
    segNote = el('div', 'lm-eb-sub');
    const offer = el('div', 'lm-eb-offer');
    offer.append(el('span', 'lm-eb-label', 'Offer it on'), segBox);
    left.append(section('Read columns from', tableSel, offer, segNote));

    // ---- the hop
    const hop = el('div', 'lm-eb-pair');
    roleSels.src = colSelect('src_col', 'Pick a column');
    roleSels.dst = colSelect('dst_col', 'Pick a column');
    hop.append(field('Source host', roleSels.src), el('span', 'lm-eb-arrow', '→'), field('Destination host', roleSels.dst));
    const strip = el('label', 'lm-eb-check');
    stripCb = el('input');
    stripCb.type = 'checkbox';
    stripCb.onchange = () => { b.strip_prefix = stripCb.checked; changed(); };
    strip.title = 'EVTX payload fields carry their field name in the value: "TargetServerName: FILESRV" becomes FILESRV.';
    strip.append(stripCb, el('span', null, 'Drop a leading "Name: " from both host values'));
    left.append(section('The hop', hop, strip));

    // ---- optional columns
    const extra = el('div', 'lm-eb-pair three');
    roleSels.label = colSelect('label_col', '(none)');
    roleSels.actor = colSelect('actor_col', '(none)');
    roleSels.time = colSelect('time_col', '(none)');
    extra.append(field('User column', roleSels.label, 'The account that moved, kept on every hop'),
      field('Acting account', roleSels.actor, 'Who used those credentials, when a row names two accounts (a 4648)'),
      field('Time column', roleSels.time, 'Puts the hops on the timeline: every view is ordered by it'));
    const fail = el('label', 'lm-eb-check');
    failCb = el('input');
    failCb.type = 'checkbox';
    failCb.onchange = () => { b.fails = failCb.checked; changed(); };
    const ft = el('span');
    ft.append('These rows are failed attempts', el('small', null, 'Drawn, but nobody arrives: switches and paths skip them, as they skip a 4625'));
    fail.append(failCb, ft);
    sessBox = el('div');
    left.append(section('Also read (optional)', extra, fail, sessBox));

    // ---- conditions
    condBox = el('div', 'lm-eb-conds');
    const addCond = el('button', 'btn ghost lm-eb-small', '+ Add a condition');
    addCond.type = 'button';
    addCond.onclick = () => { b.conditions.push({ column: '', op: 'equals', value: '' }); renderConds(); changed(); };
    left.append(section('Only rows where', condBox, addCond));

    // ---- preview
    const right = el('div', 'lm-eb-pane lm-eb-result');
    previewHead = el('div', 'lm-eb-head');
    statsBox = el('div', 'lm-eb-stats');
    notesBox = el('ul', 'lm-eb-notes');
    linksBox = el('div', 'lm-eb-links');
    right.append(previewHead, statsBox, notesBox, el('div', 'lm-eb-head', 'Busiest links'), linksBox);
    panes.append(left, right);

    // ---- name and save
    const foot = el('div', 'lm-eb-foot');
    nameInput = el('input');
    nameInput.type = 'text';
    nameInput.autocomplete = 'off';
    nameInput.id = 'lmEbName';
    if (b.nameTouched) nameInput.value = b.name;
    nameInput.oninput = () => { b.nameTouched = nameInput.value.trim() !== ''; b.name = nameInput.value; renderFoot(); };
    nameInput.onkeydown = (e) => { if (e.key === 'Enter' && !saveBtn.disabled) saveBtn.click(); };
    descInput = el('input');
    descInput.type = 'text';
    descInput.autocomplete = 'off';
    descInput.id = 'lmEbDesc';
    descInput.placeholder = 'What this hop means, shown beside it in the list';
    descInput.oninput = () => { b.description = descInput.value; };
    saveBtn = el('button', 'btn lm-eb-save', edit ? 'Save changes' : 'Save event');
    saveBtn.type = 'button';
    saveBtn.onclick = save;
    hint = el('div', 'lm-eb-hint');
    foot.append(footField('Name', nameInput), footField('Description (optional)', descInput), saveBtn, hint);

    root.append(panes, foot);
    box.append(root);
    refresh();
  }, { wide: 'x', focus: '#lmEbName' });

  function section(title, ...kids) {
    const s = el('div', 'lm-eb-sec');
    s.append(el('div', 'lm-eb-head', title), ...kids);
    return s;
  }
  function field(label, control, sub) {
    const f = el('label', 'lm-eb-field');
    f.append(el('span', 'lm-eb-label', label), control);
    if (sub) f.append(el('small', null, sub));
    return f;
  }
  function footField(label, input) {
    const f = el('div', 'lm-eb-field');
    const l = el('label', 'lm-eb-head', label);
    l.htmlFor = input.id;
    f.append(l, input);
    return f;
  }
  function colSelect(key, emptyLabel) {
    const s = el('select');
    s.dataset.role = key;
    s.dataset.empty = emptyLabel;
    s.onchange = () => { b[key] = s.value; b.picked.add(key.replace('_col', '')); fillColumns(); changed(); };
    return s;
  }
  // Options are the chosen table's columns, in the table's own order. A
  // value the table doesn't have (an event made for another case's
  // tables) stays selected and says so, rather than silently becoming
  // the first column.
  function fillSelect(s, value, emptyLabel) {
    const src = srcOf();
    s.replaceChildren(new Option(emptyLabel, ''));
    for (const c of src.columns) s.append(new Option(c.name, c.name));
    if (value && !hasCol(value)) s.append(new Option(`${value} (not in this table)`, value));
    s.value = value || '';
    s.classList.toggle('missing', !!value && !hasCol(value));
  }
  function fillColumns() {
    for (const s of Object.values(roleSels)) fillSelect(s, b[s.dataset.role], s.dataset.empty);
    stripCb.checked = b.strip_prefix;
    failCb.checked = b.fails;
    paintSession();
  }
  // A session rule (the shipped RDP event's 21 and 25 opening a session,
  // 24 closing it) has no editor of its own: it comes with the event it
  // was copied from, and can be dropped.
  function paintSession() {
    sessBox.replaceChildren();
    if (!b.session) return;
    const lab = el('label', 'lm-eb-check');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = b.keepSession;
    cb.onchange = () => { b.keepSession = cb.checked; changed(); };
    const r = b.session;
    const t = el('span');
    t.append(`Draw sessions: ${r.column} ${r.open.join(' or ')} opens one, ${r.close.join(' or ')} closes it`,
      el('small', null, `From the event this started from. Untick to draw each row as a hop only.${hasCol(r.column) ? '' : ` This table has no ${r.column} column.`}`));
    lab.append(cb, t);
    sessBox.append(lab);
  }
  function paintBinding() {
    const src = srcOf();
    const sets = headerSetsOf(src);
    if (b.binding && !sets.includes(b.binding)) sets.push(b.binding);
    segBox.replaceChildren();
    const opts = [...sets.map((n) => [n, `${n} tables`]), ['', 'Any table with these columns']];
    for (const [value, label] of opts) {
      const btn = el('button', null, label);
      btn.type = 'button';
      btn.setAttribute('aria-pressed', String(b.binding === value));
      btn.onclick = () => { b.binding = value; b.bindingTouched = true; changed(); };
      segBox.append(btn);
    }
    const def = draftDef();
    const cols = requiresOf(def);
    const here = sources.filter((s) => bindsTo(def, s));
    const names = here.slice(0, 3).map((s) => s.name).join(', ') + (here.length > 3 ? ` and ${here.length - 3} more` : '');
    segNote.textContent = (b.binding
      ? `Offered on every table that has the ${b.binding} columns`
      : `Offered on every table that has ${cols.length ? cols.join(', ') : 'the columns you pick'}`)
      + (here.length ? `. In this case: ${names}.` : '. No table in this case has them all.');
  }

  function renderConds() {
    condBox.replaceChildren();
    if (!b.conditions.length) {
      condBox.append(el('p', 'lm-eb-empty', 'Every row with both hosts counts. Add a condition to narrow it, for example EventId is 4624.'));
      return;
    }
    b.conditions.forEach((c) => {
      const row = el('div', 'lm-eb-cond');
      const col = el('select');
      col.setAttribute('aria-label', 'Condition column');
      fillSelect(col, c.column, 'Pick a column');
      const op = el('select');
      op.setAttribute('aria-label', 'Condition operator');
      for (const [o, label] of OP_LABELS) op.append(new Option(label, o));
      op.value = c.op || 'equals';
      const val = el('input');
      val.type = 'text';
      val.setAttribute('aria-label', 'Condition value');
      val.value = c.value ?? '';
      val.placeholder = LIST_OPS.has(op.value) ? 'value, value, …' : 'value';
      const rm = el('button', 'btn ghost lm-eb-x', '✕');
      rm.type = 'button';
      rm.title = 'Remove this condition';
      const chips = el('div', 'lm-eb-vals');
      col.onchange = () => { c.column = col.value; col.classList.toggle('missing', false); loadValues(c, chips, val); changed(); };
      op.onchange = () => {
        c.op = op.value;
        val.placeholder = LIST_OPS.has(op.value) ? 'value, value, …' : 'value';
        paintChips(c, chips, val);
        changed();
      };
      val.oninput = () => { c.value = val.value; paintChips(c, chips, val); changed(); };
      rm.onclick = () => { b.conditions.splice(b.conditions.indexOf(c), 1); renderConds(); changed(); };
      row.append(col, op, val, rm, chips);
      condBox.append(row);
      loadValues(c, chips, val);
    });
  }

  // The commonest values of a condition's column, as chips: a click sets
  // the value, or toggles it in and out of an "is one of" list.
  function loadValues(c, chips, input) {
    chips.replaceChildren();
    if (!c.column || !hasCol(c.column)) return;
    const key = `${b.sourceId}::${c.column}`;
    if (!valueCache.has(key)) {
      valueCache.set(key, post(`${winnow.base}/values`, { source_id: b.sourceId, column: c.column })
        .then((r) => r.values).catch(() => []));
    }
    valueCache.get(key).then((vals) => { chips._vals = vals; paintChips(c, chips, input); });
  }
  function paintChips(c, chips, input) {
    const vals = chips._vals || [];
    chips.replaceChildren();
    const list = LIST_OPS.has(c.op);
    const picked = list ? listOf(c.value) : [String(c.value).trim()];
    for (const { value, n } of vals.slice(0, 8)) {
      const chip = el('button', 'lm-eb-chip');
      chip.type = 'button';
      chip.title = `${value} — ${n.toLocaleString()} rows`;
      chip.setAttribute('aria-pressed', String(picked.includes(value)));
      chip.append(el('b', null, value), document.createTextNode(` ${n.toLocaleString()}`));
      chip.onclick = () => {
        if (list) {
          const next = picked.includes(value) ? picked.filter((v) => v !== value) : [...picked, value];
          c.value = next.join(', ');
        } else c.value = value;
        input.value = c.value;
        paintChips(c, chips, input);
        changed();
      };
      chips.append(chip);
    }
  }

  // The definition as it would be saved right now.
  function draftDef() {
    const def = {
      name: currentName(), src_col: b.src_col, dst_col: b.dst_col,
      label_col: b.label_col || null, actor_col: b.actor_col || null, time_col: b.time_col || null,
      strip_prefix: b.strip_prefix,
      header_set: b.binding || null,
      conditions: usableConditions().map((c) => ({ column: c.column, op: c.op, value: String(c.value) })),
    };
    if (b.fails) def.fails = true;
    if (b.session && b.keepSession) def.session = b.session;
    if (b.description.trim()) def.description = b.description.trim();
    def.requires = requiresOf(def);
    return def;
  }

  function changed() {
    paintBinding();
    renderFoot();
    clearTimeout(timer);
    timer = setTimeout(runPreview, 180);
  }
  function refresh() {
    tableSel.value = String(b.sourceId);
    fillColumns();
    renderConds();
    descInput.value = b.description;
    if (b.nameTouched) nameInput.value = b.name;
    changed();
  }
  function renderFoot() {
    if (!b.nameTouched) nameInput.value = suggestedName();
    const p = problem();
    hint.textContent = p;
    saveBtn.disabled = !!p;
  }

  async function runPreview() {
    const my = ++seq;
    const src = srcOf();
    previewHead.textContent = `What it finds in ${src.name}`;
    const missing = requiresOf(draftDef()).filter((c) => !hasCol(c));
    if (!b.src_col || !b.dst_col) return paintPreview(null, ['Pick a source and a destination column to see the links this event finds.']);
    if (b.src_col === b.dst_col) return paintPreview(null, ['Source and destination are the same column, so every row would be a host reaching itself.'], 'warn');
    if (missing.length) return paintPreview(null, [`This table has no ${missing.join(', ')} column. Pick another table to preview against.`], 'warn');
    statsBox.classList.add('busy');
    try {
      // the address map of what the views draw now, so a flow export's
      // addresses fold to the names the event logs give them, here as
      // they will once it is saved
      const r = await post(`${winnow.base}/preview`, { selection: { source_id: b.sourceId, ...draftDef() }, ipmap: IPMAP });
      if (my === seq) paintPreview(r);
    } catch (e) {
      if (my === seq) paintPreview(null, [e.message], 'warn');
    }
    return undefined;
  }

  // Set against the hops the views draw now, less this event's own: the
  // links only it draws, and its rows with no account that back up a hop
  // another event already draws (same two hosts, within a minute).
  function against(r) {
    const skip = edit ? edit.name : null;
    const others = HOPS.filter((h) => EV[h.k] && EV[h.k].name !== skip);
    if (!others.length) return null;
    const known = new Set(others.map((h) => `${HOSTS[h.s]}|${HOSTS[h.d]}`));
    const named = new Set(others.filter((h) => h.u !== NOACCT).map((h) => `${HOSTS[h.s]}|${HOSTS[h.d]}|${T0M + h.t}`));
    let backs = 0;
    const fresh = new Set();
    for (const [s, d, u, m, n] of r.drawn) {
      if (!known.has(`${s}|${d}`)) fresh.add(`${s}|${d}`);
      if (!u && [0, -1, 1].some((dt) => named.has(`${s}|${d}|${m + dt}`))) backs += n;
    }
    return { backs, fresh: [...fresh].sort().map((k) => k.split('|')) };
  }

  function paintPreview(r, msgs = [], kind = 'info') {
    statsBox.classList.remove('busy');
    statsBox.replaceChildren();
    const stat = (v, label) => {
      const s = el('div', 'lm-eb-stat');
      s.append(el('b', null, v == null ? '—' : v.toLocaleString()), el('span', null, label));
      statsBox.append(s);
    };
    stat(r && r.matched, 'rows match');
    stat(r && r.links, r && r.links_capped ? 'links (or more)' : 'links');
    stat(r && r.hosts, 'hosts');
    notesBox.replaceChildren();
    const note = (content, k = 'info') => { const li = el('li', k); li.append(...[].concat(content)); notesBox.append(li); };
    for (const m of msgs) note(m, kind);
    linksBox.replaceChildren();
    if (!r) { linksBox.append(el('p', 'lm-eb-empty', 'Nothing to show yet.')); return; }
    for (const c of waitingConditions()) note(`The ${c.column} condition has no value yet, so this leaves it out.`);

    if (!r.matched) {
      note(usableConditions().length
        ? 'No rows in this table match the conditions. The value chips under each condition show how the column is actually spelled.'
        : 'This table is empty.', 'warn');
    } else if (r.matched > r.hops) {
      note(`${(r.matched - r.hops).toLocaleString()} of the ${r.matched.toLocaleString()} matching rows draw nothing: `
        + 'the source or destination is empty, "-", loopback, or the same host on both ends.');
    }
    const vs = r.hops ? against(r) : null;
    if (r.hops && r.bare) {
      const backs = vs ? vs.backs : 0, alone = r.bare - backs;
      note(`${b.label_col ? `${plural(r.bare, 'row')} with an empty ${b.label_col} carry` : 'No user column, so these hops carry'} no account. `
        + (backs ? `${backs.toLocaleString()} of ${b.label_col ? 'them' : `the ${r.bare.toLocaleString()}`} line up with a hop another event already draws (same two hosts, within a minute) and back it up instead of drawing again; ` : '')
        + `${!backs ? 'They draw on their own' : alone === 1 ? '1 draws on its own' : `${alone.toLocaleString()} draw on their own`}. `
        + 'By account leaves them out, and a path step next to one reads "no account to check".');
    }
    if (r.folded.length) {
      const [ip, name] = r.folded[0];
      const sentence = [`${r.folded.length === 1 ? '1 address folds' : `${r.folded.length.toLocaleString()} addresses fold`} to the host name a row puts beside it (`,
        el('span', 'mono', ip), ' is ', el('span', 'mono', name), ')'];
      if (r.addresses.length) {
        sentence.push(`; ${r.addresses.length === 1 ? 'one stays an address' : `${r.addresses.length.toLocaleString()} stay addresses`}: `,
          el('span', 'mono', r.addresses.slice(0, 3).join(', ') + (r.addresses.length > 3 ? ', …' : '')));
      }
      sentence.push('.');
      note(sentence, 'ok');
    }
    if (vs && vs.fresh.length) {
      const first = vs.fresh.slice(0, 3).map(([s, d]) => `${s} → ${d}`).join(', ');
      note([`${plural(vs.fresh.length, 'link')} no other ticked event draws${r.drawn_capped ? ' (in the first rows read)' : ''}: `,
        el('span', 'mono', first + (vs.fresh.length > 3 ? ', …' : '')), '.'], 'new');
    }
    if (r.hops) {
      if (!b.time_col) note('No time column, so the views will not show this event: every one of them is ordered by time.', 'warn');
      else if (r.first) note(`Hops from ${r.first} to ${r.last}${r.untimed ? `; ${plural(r.untimed, 'row')} with no time the views can place` : ''}.`, 'ok');
      else note(`${b.time_col} holds nothing that reads as a time in these rows, so the views will not show this event.`, 'warn');
    }

    if (!r.top.length) { linksBox.append(el('p', 'lm-eb-empty', 'No links.')); return; }
    const t = el('table', 'lm-eb-table');
    const head = el('tr');
    for (const h of ['Source', '', 'Destination', 'Rows', ...(b.label_col ? [b.label_col] : [])]) head.append(el('th', null, h));
    const thead = el('thead');
    thead.append(head);
    const body = el('tbody');
    const max = r.top[0].n;
    const freshKeys = new Set(vs ? vs.fresh.map(([s, d]) => `${s}|${d}`) : []);
    // the folded name, and how this table writes it when that differs
    const hostCell = (name, raw) => {
      const td = el('td', 'host', name);
      const written = String(raw ?? '').trim();
      if (written && written.toUpperCase() !== name) td.append(el('small', null, written));
      td.title = written && written !== name ? `${name}, written ${written}` : name;
      return td;
    };
    for (const e of r.top) {
      const tr = el('tr');
      if (freshKeys.has(`${e.src}|${e.dst}`)) tr.className = 'fresh';
      const bar = el('td', 'num');
      const fill = el('span', 'lm-eb-bar');
      fill.style.width = `${Math.max(4, (e.n / max) * 100)}%`;
      bar.append(fill, el('span', null, e.n.toLocaleString()));
      tr.append(hostCell(e.src, e.raw_src), el('td', 'arrow', '→'), hostCell(e.dst, e.raw_dst), bar);
      if (b.label_col) tr.append(el('td', 'num', e.labels == null ? '' : e.labels.toLocaleString()));
      body.append(tr);
    }
    t.append(thead, body);
    linksBox.append(t);
    if (r.links > r.top.length) {
      linksBox.append(el('p', 'lm-eb-empty', `and ${plural(r.links - r.top.length, 'more link')}`));
    }
  }

  async function save() {
    if (problem()) return;
    const def = draftDef();
    saveBtn.disabled = true;
    closeModal();
    await onSave(def);
    if (onDone) onDone();
  }
}
