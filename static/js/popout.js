/* Pop-out windows: one page tab — SQL, Timeline, Notes, Watchlist, a plugin
   tab, a pinned dashboard — on its own in a window beside the main one, so
   the Pivot can sit next to the table it counts instead of taking turns
   with it.

   A pop-out is the whole app, booted on the same server and the same case
   at /?popout=<page key>&owner=<main window id>, with the main window's
   chrome hidden (html[data-popout], style.css) and that one page shown
   (enterPopout, from boot). It is deliberately not the main window's DOM
   rendered into a child window: that would share S, but every menu, dialog
   and toast the page opens would land back in the main window, and so
   would every document-level listener a plugin adds.

   The server needs nothing new for it. Any number of windows on one case
   was already safe — per-window presence, the case token on every request
   (docs/notes/server.md) — so a pop-out is just another window. What it
   adds is in the browser, and it comes down to four rules:

   - ONE LIVE COPY. While a page is out, the main window never mounts it:
     its tab shows #popoutPlaceholder instead (pageElsewhere, at the top of
     every page's show function). Two Pivots would both save to the same
     winnow.tabState row, and the last one to write would win — silently.
     Saves still in their debounce are written before the page moves
     (flushPageSaves), because the window it moves to reads them back as
     it mounts.
   - THE GRID IS THE MAIN WINDOW'S. A pop-out has no grid and no tabs.
     Opening a table, filtering it, jumping to a row, showing another page,
     setting the timeframe — from a pop-out, each is sent to the main
     window (sendToMain → ACTIONS), whole: opening the table there and then
     filtering here would filter nothing anyone can see.
   - EACH WINDOW HAS ITS OWN COPY OF THE CASE, so writes are announced.
     core.js tells this module about every request that could have changed
     something (onMutation); the ones that changed case data are named
     (MUTATION_KINDS) and broadcast, and the other windows reload what they
     hold and repaint the page they show (runRefresh). The few changes no
     route announces — a job finishing, a watchlist scan, a plugin's own
     route making a table — call announceChange themselves.
   - A POP-OUT BELONGS TO THE MAIN WINDOW THAT OPENED IT. Messages carry
     that window's id (`family`), so a second main window on the same
     server neither drives nor hears another's pop-outs. The id lives in
     sessionStorage, so a reloaded main window is still the owner: its
     pop-outs wait OWNER_GRACE_MS for it to say hello again, and close if
     it doesn't — closing the main window closes its pop-outs. A case
     switch is followed rather than refused (connection.js caseGone):
     the pop-out reloads onto the case now open.

   The windows talk over a BroadcastChannel: every window on this Winnow's
   origin shares it, no server round trip, and it survives a reload on
   either side, which a window handle does not. The handle window.open
   returns is still kept where there is one: it is what can bring a pop-out
   to the front, and `.closed` is how a window that went without saying
   goodbye is noticed. */
import { $, el, onMutation, toast } from './core.js';
import { drillInto, markDashboardStale, showDashboard } from './dashboard.js';
import { render } from './grid.js';
import { drawRail, regroupIfGroupedByTag } from './grouping.js';
import { flushNotesSave, loadRowNotes, resetNotes } from './notes.js';
import { dropPluginTabMount, flushPendingTabState, openFilteredRows, setCaseTimeRange, showPage, showPluginTab } from './plugins.js';
import { loadCaseVariables } from './savedfilters.js';
import { closePageTab, loadSources, openSource, pageTabs, renderPageTabs, syncTabSelection } from './sources.js';
import { flushSqlTabSave, gridIsShowing, showMainView, syncTabChrome } from './sql.js';
import { S } from './state.js';
import { recordTabVisit } from './tabhistory.js';
import { clearRowCaches, loadCaseTags, loadTags } from './tags.js';
import { buildTimeline, jumpToTimelineRow } from './timeline.js';
import { contextMenu } from './ui.js';
import { refreshWatchlistBadge, showWatchlistTab } from './watchlist.js';

const CHANNEL = 'winnow.windows';
const WINDOW_ID_KEY = 'winnow.windowId';   // sessionStorage: per tab, survives a reload
const GEOMETRY_KEY = 'winnow.popouts';      // localStorage: each page's last pop-out size and place
// How long a pop-out waits for its main window to come back from a reload
// before taking the window's going as for good. A reload says hello well
// inside this; a close never does.
const OWNER_GRACE_MS = 4000;
// "Bring it back as a tab" asks the pop-out to put itself away (it has
// saves to make first); one that doesn't answer in this is taken back anyway.
const DOCK_TIMEOUT_MS = 2000;
// A pop-out closed with the window's ✕ gets this long to land the saves its
// pagehide sent before the main window mounts the page from them.
const GONE_SETTLE_MS = 800;
const WATCH_MS = 1000;
const ANNOUNCE_MS = 120;
const REFRESH_MS = 250;

/* The writes the other windows have to hear about, by route. An allowlist:
   most POSTs here are reads with a body (view builds, previews, SQL runs)
   or this window's own bookkeeping (layouts, tab state, SQL tab text), and
   a refresh in every other window per keystroke of those would be the
   bill. A route that changes tags, tables or notes and isn't named here
   leaves the other windows stale until they next load — so a new one is
   added here. The kinds decide what the listener reloads (runRefresh). */
const MUTATION_KINDS = [
  [/^\/api\/(row_tags|tags)(\/|\?|$)/, 'tags'],
  [/^\/api\/note(\?|$)/, 'notes'],
  [/^\/api\/(source\/|merges(?!\/preview)|folders|tabs\/|view\/save_as_table|sql\/to_table|case\/copy_sources)/, 'sources'],
  [/^\/api\/derived(\/batch|\/\d+(\/rederive|\/rename)?)?(\?|$)/, 'sources'],
  [/^\/api\/watchlist(?!\/(scan|seen))/, 'watchlist'],
  [/^\/api\/(dashboards(?!\/\d+\/refresh)|dashboard_library\/\d+\/add|plugin_dashboards\/)/, 'dashboards'],
  [/^\/api\/case\/variables/, 'variables'],
];

let channel = null;
let parsed = false;
let ownKey = null;          // pop-out: the page this window shows
let ownerId = null;         // pop-out: the main window that opened it
let myId = null;            // main: this window's id (windowId)
const popped = new Map();   // main: page key -> { win } for each page out in a pop-out
let watchTimer = null;      // main: notices a pop-out whose window closed without a word
let ownerGone = null;       // pop-out: armed when the main window said it was leaving
let docking = false;        // pop-out: going back on purpose, so no "gone" on the way out
let following = false;      // pop-out: reloading onto the case now open, so no "gone" either
let pendingAnnounce = new Set();
let announceTimer = null;
let pendingRefresh = new Set();
let refreshTimer = null;

function readParams() {
  if (parsed) return;
  parsed = true;
  const q = new URLSearchParams(location.search);
  ownKey = q.get('popout') || null;
  ownerId = ownKey ? q.get('owner') : null;
}

/* The page this window was popped out to show, or null in a main window. */
export function popoutPage() { readParams(); return ownKey; }
export function inPopout() { return !!popoutPage(); }

/* This main window's id. sessionStorage, so it is per tab and outlives a
   reload — which is what lets a reloaded main window keep its pop-outs. A
   pop-out never asks: window.open copies the opener's sessionStorage into
   the new window, so it would only find its owner's id there. */
function windowId() {
  if (myId) return myId;
  try { myId = sessionStorage.getItem(WINDOW_ID_KEY); } catch { /* storage off: one id per load */ }
  if (!myId) {
    myId = `w${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
    try { sessionStorage.setItem(WINDOW_ID_KEY, myId); } catch { /* see above */ }
  }
  return myId;
}

const family = () => (inPopout() ? ownerId : windowId());
const me = () => (inPopout() ? `po:${ownKey}` : 'main');

function send(msg) {
  if (!channel) return;
  try { channel.postMessage({ ...msg, family: family(), from: me() }); }
  catch (e) { console.error('popout: could not send', msg.type, e); }
}

/* Whether a page is out in a pop-out of this window. */
export function isPoppedOut(key) { return popped.has(key); }

/* This window's pop-outs — what Shut down leaves out of "other windows". */
export function ownPopoutCount() { return inPopout() ? 0 : popped.size; }

/* ------------------------------------------------------ the main window */

const safeName = (key) => key.replace(/[^\w-]/g, '_');
const windowName = (key) => `winnow-${windowId()}-${safeName(key)}`;
const popoutUrl = (key) => `/?popout=${encodeURIComponent(key)}&owner=${encodeURIComponent(windowId())}`;

function loadGeometry() {
  try { return JSON.parse(localStorage.getItem(GEOMETRY_KEY) || '{}') || {}; } catch { return {}; }
}

/* Where the window opens: where this page's pop-out was last, else beside
   the main window when the screen has room for it there, else over the
   main window's right edge. A browser keeps a new window on its opener's
   screen unless it has been allowed to place windows across displays, so
   a size and place saved on a second monitor can come back on the first. */
function features(key) {
  const g = loadGeometry()[key];
  const w = g ? g.w : Math.round(Math.min(Math.max(window.outerWidth * 0.5, 640), 1100));
  const h = g ? g.h : Math.max(window.innerHeight, 560);
  let x = g ? g.x : window.screenX + window.outerWidth;
  const y = g ? g.y : window.screenY;
  if (!g) {
    const scr = window.screen || {};
    const right = (scr.availLeft || 0) + (scr.availWidth || 0);
    if (scr.availWidth && x + w > right) x = Math.max(scr.availLeft || 0, window.screenX + window.outerWidth - w);
  }
  return `popup=yes,width=${Math.round(w)},height=${Math.round(h)},left=${Math.round(x)},top=${Math.round(y)}`;
}

/* What this window was about to save for the page, saved now. The window
   the page is going to reads it back as it mounts. `keepalive` when this
   window is closing: the requests have to outlive it. */
async function flushPageSaves(key, { keepalive = false } = {}) {
  if (key.startsWith('plugin:')) await flushPendingTabState({ keepalive });
  else if (key === 'sql') await flushSqlTabSave({ keepalive });
  else if (key === 'notes') await flushNotesSave({ keepalive });
}

/* Pop a page out into its own window. Called from a click (the page tab's
   menu, the sidebar's ⧉), and the window is opened before anything is
   awaited: a browser lets a page open a window only while the click that
   asked for it is still fresh, and refuses it as a pop-up otherwise. It
   opens blank and is pointed at the page once this window's saves for it
   have landed, so the pop-out can never read what was there before them. */
export async function popOut(key) {
  if (inPopout()) return;
  if (popped.has(key)) { showPopout(key); return; }
  const t = pageTabs().find((p) => p.key === key);
  if (!t) return;
  const win = window.open('', windowName(key), features(key));
  if (!win) {
    toast('The browser blocked the new window — allow pop-ups for Winnow, then try again', 7000);
    return;
  }
  popped.set(key, { win });
  const wasShowing = pageShowing(key);
  try { await flushPageSaves(key); } catch (e) { console.error(e); }
  if (key.startsWith('plugin:')) dropPluginTabMount(key.slice(7));
  win.location.replace(popoutUrl(key));
  paintPopped();
  watchPopouts();
  // The point of popping the page out is to see it beside the table, so
  // the main window goes back to the table when there is one to go to.
  if (wasShowing) {
    if (S.sourceId != null && S.sources.some((s) => s.id === S.sourceId && s.is_open)) openSource(S.sourceId);
    else showPlaceholder(key);
  }
}

/* Bring a page's pop-out to the front. A main window reloaded since it
   opened the pop-out holds no handle to it; the window's name finds it
   again — asked only on a click, and only for a pop-out that has said it
   is there, so this finds the window rather than opening a blank one. If
   it had gone after all, the blank one is pointed at the page. */
export function showPopout(key) {
  const rec = popped.get(key);
  if (!rec) return;
  let w = rec.win;
  if (!w || w.closed) {
    w = window.open('', windowName(key), features(key));
    if (!w) { toast('The browser blocked the window — allow pop-ups for Winnow', 6000); return; }
    try { if (w.location.href === 'about:blank') w.location.replace(popoutUrl(key)); } catch { /* not ours to read: leave it */ }
    rec.win = w;
    watchPopouts();
  }
  try { w.focus(); } catch { /* best effort */ }
}

/* "Bring it back as a tab": the pop-out puts itself away (it has saves to
   make first, and only it can make them); one that doesn't answer is
   taken back anyway. */
function requestDock(key) {
  if (!popped.has(key)) return;
  send({ type: 'dock-request', key });
  setTimeout(() => {
    const rec = popped.get(key);
    if (!rec) return;
    try { if (rec.win && !rec.win.closed) rec.win.close(); } catch { /* best effort */ }
    returnPage(key);
  }, DOCK_TIMEOUT_MS);
}

/* The page is this window's again: forget what it held of it, and if its
   placeholder is what is showing, show the page in its place. */
function returnPage(key, { delay = 0 } = {}) {
  if (!popped.delete(key)) return;
  // The other window wrote to it; what is held here is from before.
  if (key === 'notes') resetNotes();
  if (key.startsWith('plugin:')) dropPluginTabMount(key.slice(7));
  paintPopped();
  if (!placeholderShowing(key)) return;
  setTimeout(() => {
    if (!placeholderShowing(key) || popped.has(key)) return;
    const t = pageTabs().find((p) => p.key === key);
    if (t) t.show();
  }, delay);
}

function placeholderShowing(key) {
  const ph = $('popoutPlaceholder');
  return !!ph && !ph.hidden && ph.dataset.key === key;
}

function pageShowing(key) {
  if (placeholderShowing(key)) return false;
  if (key.startsWith('dashboard:')) return S.activeTab === 'dashboard' && `dashboard:${S.dashboardId}` === key;
  return S.activeTab === key;
}

/* The tab strip's ⧉ marks, and the sidebar's (renderPageTabs ends in
   syncTabSelection, which repaints the sidebar). */
function paintPopped() {
  renderPageTabs();
}

/* A pop-out closed without a goodbye — the browser killed it, or its
   pagehide message was lost — is noticed through its handle. Only while
   there are handles to watch. */
function watchPopouts() {
  if (watchTimer) return;
  watchTimer = setInterval(() => {
    let watching = false;
    for (const [key, rec] of [...popped]) {
      if (!rec.win) continue;
      if (rec.win.closed) returnPage(key, { delay: GONE_SETTLE_MS });
      else watching = true;
    }
    if (!watching) { clearInterval(watchTimer); watchTimer = null; }
  }, WATCH_MS);
}

/* The card #popoutPlaceholder shows: in the main window, where a page is
   that's out; in a pop-out, why it has nothing to show. */
function paintCard(title, text, buttons) {
  const box = $('popoutPlaceholder');
  const card = el('div', 'popout-card');
  card.append(el('div', 'popout-card-mark', '⧉'), el('div', 'popout-card-title', title), el('p', null, text));
  if (buttons.length) {
    const acts = el('div', 'popout-card-actions');
    acts.append(...buttons);
    card.append(acts);
  }
  box.replaceChildren(card);
  showMainView('popoutPlaceholder');
}

function showPlaceholder(key) {
  const t = pageTabs().find((p) => p.key === key);
  const label = t ? t.label : key;
  if (key.startsWith('dashboard:')) {
    S.activeTab = 'dashboard';
    S.dashboardId = Number(key.slice('dashboard:'.length));
  } else {
    S.activeTab = key;
  }
  const show = el('button', 'btn primary', `Show the ${label} window`);
  show.onclick = () => showPopout(key);
  const back = el('button', 'btn ghost', 'Bring it back as a tab');
  back.onclick = () => requestDock(key);
  paintCard(`${label} is open in its own window`,
    'It follows this case and this window. Bring it back, or close that window, to see it here again.',
    [show, back]);
  $('popoutPlaceholder').dataset.key = key;
  syncTabSelection();
  syncTabChrome();
  show.focus();
}

/* At the top of every page's show function: true when this window must not
   show the page itself, having done what it should instead. In the main
   window, that is a page out in a pop-out — its placeholder goes up. In a
   pop-out, it is any page but its own, which is the main window's to show. */
export function pageElsewhere(key) {
  if (inPopout()) {
    if (key === ownKey) return false;
    // The query the pop-out picked (a dashboard's "open as query" names it
    // before showing SQL) travels with it.
    sendToMain('showPage', key, key === 'sql' ? { sqlTabId: S.sqlTabId } : null);
    return true;
  }
  if (!popped.has(key)) return false;
  recordTabVisit({ kind: 'page', key: key.startsWith('plugin:') ? key.slice('plugin:'.length) : key });
  showPlaceholder(key);
  return true;
}

/* Right-click on a page tab, or its row in the sidebar. */
export function openPageTabMenu(e, key) {
  e.preventDefault();
  if (inPopout()) return;
  const out = popped.has(key);
  const r = e.currentTarget.getBoundingClientRect();
  const onNode = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
  const at = e.button === -1 || !onNode ? { clientX: r.left, clientY: r.bottom } : e;
  contextMenu(at, () => [
    out
      ? { label: 'Show its window', title: 'This page is open in a window of its own', onclick: () => showPopout(key) }
      : { label: 'Open in its own window', title: 'Pop this page out into a window beside this one', onclick: () => popOut(key) },
    ...(out ? [{ label: 'Bring it back as a tab', onclick: () => requestDock(key) }] : []),
    // What the sidebar row's ✕ does: a pinned board's close is an unpin.
    key.startsWith('dashboard:')
      ? { label: 'Unpin this board', title: 'Stays in the case, under the sidebar’s Dashboards', onclick: () => closePageTab(key) }
      : { label: 'Close this tab', title: 'Stays in the case — reopen it from the sidebar’s Pages', onclick: () => closePageTab(key) },
  ]);
}

/* What a pop-out can ask the main window to do. Each one is the WHOLE
   navigation, since the main window is where every part of it shows. */
const ACTIONS = {
  openSource: (id) => openSource(id),
  openFiltered: (sourceId, pairs) => openFilteredRows(sourceId, pairs),
  jumpToRow: (sourceId, rid) => jumpToTimelineRow(sourceId, rid),
  drill: (w, extra) => drillInto(w, extra),
  setTimeRange: (range) => setCaseTimeRange(range),
  showPage: async (key, extra) => {
    if (popped.has(key)) { showPopout(key); return; }
    if (key === 'sql' && extra && extra.sqlTabId != null) S.sqlTabId = extra.sqlTabId;
    if (key.startsWith('dashboard:')) { await showDashboard(Number(key.slice('dashboard:'.length))); return; }
    await showPage(key);
  },
};

/* --------------------------------------------------------- a pop-out */

/* Ask the main window to do something — see ACTIONS. Its window is
   brought forward while the click that asked is still fresh enough to be
   allowed to. */
export function sendToMain(name, ...args) {
  if (!inPopout()) return false;
  send({ type: 'act', name, args });
  try { if (window.opener && !window.opener.closed) window.opener.focus(); } catch { /* best effort */ }
  return true;
}

/* The rest of boot, for a pop-out — the case is open and its tags are
   loaded. The source list without a table opened under it (a page is up,
   which is what loadSources checks), then the one page. */
export async function enterPopout(cur) {
  const key = popoutPage();
  const caseName = cur.temp ? 'Quick look' : cur.name;
  S.activeTab = key;
  await loadSources(undefined, { navigate: false });
  const t = pageTabs().find((p) => p.key === key);
  if (!t) {
    // A board of the case just left, a plugin since switched off.
    paintCard('This page isn’t in this case',
      `Winnow now has “${caseName}” open, which has no page here to show. Close this window and pop the page out again from the main window.`,
      [closeButton()]);
    send({ type: 'gone', key });
    return;
  }
  $('popoutPage').textContent = t.label;
  $('popoutCase').textContent = caseName;
  $('popoutCase').title = cur.path || '';
  $('popoutBar').hidden = false;
  document.title = `${t.label} — ${caseName} · Winnow`;
  send({ type: 'here', key });
  await t.show();
}

/* The main window switched case (connection.js caseGone). The pop-out
   reloads onto the case now open rather than covering itself the way a
   main window does — and says nothing on the way out, since it is coming
   straight back: "gone" would have the main window mount the page for the
   moment until it says "here" again. If the page isn't in the new case,
   enterPopout says "gone" then. */
export function followCaseSwitch() {
  following = true;
  location.reload();
}

/* Boot found no case open — the main window closed it, or the server was
   restarted without one. */
export function popoutWithoutCase() {
  $('home').hidden = true;
  $('app').hidden = false;
  paintCard('Winnow has no case open',
    'This window showed a page of a case that is no longer open. Close it, open the case in the main window, and pop the page out again.',
    [closeButton()]);
  send({ type: 'gone', key: popoutPage() });
}

function closeButton() {
  const b = el('button', 'btn', 'Close this window');
  b.onclick = () => window.close();
  return b;
}

/* "Back to main window": save what is pending, tell the main window to
   show the page, close. The order is the point — the main window mounts
   the page from what was saved. */
async function dock() {
  if (docking) return;
  docking = true;
  try { await flushPageSaves(ownKey); } catch (e) { console.error(e); }
  send({ type: 'dock', key: ownKey });
  try { if (window.opener && !window.opener.closed) window.opener.focus(); } catch { /* best effort */ }
  closeSelf();
}

/* window.close() is refused for a window no script opened — this page
   loaded by hand, say — so say what to do instead. */
function closeSelf() {
  window.close();
  setTimeout(() => {
    if (window.closed) return;
    paintCard('This window is done',
      'Its page is back in the main window. Close this window to finish.', []);
  }, 300);
}

/* The main window is gone for good, so its pop-outs go too: they are
   pages of it. What the page had pending is saved on the way. */
function ownerClosed() {
  if (docking) return;
  docking = true;
  flushPageSaves(ownKey, { keepalive: true }).catch((e) => console.error(e));
  closeSelf();
}

/* The other way the main window's going is noticed: "leaving" is said on
   pagehide, which a window killed rather than closed never fires. The
   handle window.open left this window is told either way — and a reload
   of the main window leaves it open, so this never fires for one. */
function watchOwner() {
  let opener = null;
  try { opener = window.opener; } catch { /* not ours to read */ }
  if (!opener) return;
  const timer = setInterval(() => {
    let gone = true;
    try { gone = !window.opener || window.opener.closed; } catch { /* see above */ }
    if (gone) { clearInterval(timer); ownerClosed(); }
  }, WATCH_MS);
}

function savePlacement() {
  try {
    const all = loadGeometry();
    all[ownKey] = { w: window.innerWidth, h: window.innerHeight, x: window.screenX, y: window.screenY };
    localStorage.setItem(GEOMETRY_KEY, JSON.stringify(all));
  } catch { /* a convenience, not state */ }
}

/* ------------------------------------------ every window: what changed */

/* Tell the other windows of this family that case data changed. Coalesced:
   tagging a run of rows is a run of requests. */
export function announceChange(kinds) {
  for (const k of kinds) pendingAnnounce.add(k);
  if (announceTimer) return;
  announceTimer = setTimeout(() => {
    announceTimer = null;
    const list = [...pendingAnnounce];
    pendingAnnounce = new Set();
    if (list.length) send({ type: 'changed', kinds: list });
  }, ANNOUNCE_MS);
}

function onMutationSent(path) {
  const bare = String(path).split('#')[0];
  const kinds = MUTATION_KINDS.filter(([re]) => re.test(bare)).map(([, k]) => k);
  if (kinds.length) announceChange(kinds);
}

function queueRefresh(kinds) {
  for (const k of kinds || []) pendingRefresh.add(k);
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(runRefresh, REFRESH_MS);
}

/* Another window wrote. Reload what this one holds of it, then repaint
   what is showing — the grid's rows the way every other tag path ends
   (watchlist.js finishScan says why the repaint waits for a showing
   grid), or the page, the way a visit to its tab would. */
async function runRefresh() {
  const kinds = pendingRefresh;
  pendingRefresh = new Set();
  const any = (...ks) => ks.some((k) => kinds.has(k));
  try {
    // With a table open, the main window's ribbon counts that table's rows
    // too (loadTags); a pop-out, which has none, reads the definitions.
    if (any('tags')) await (!inPopout() && S.sourceId != null ? loadTags() : loadCaseTags());
    if (any('sources', 'dashboards')) await loadSources(undefined, { navigate: false });
    if (any('variables')) await loadCaseVariables();
    if (!inPopout()) {
      if (any('watchlist', 'tags')) refreshWatchlistBadge();
      if (any('tags', 'notes') && S.view) {
        clearRowCaches();
        if (gridIsShowing()) { render(); drawRail(); regroupIfGroupedByTag(); } else S.gridRepaintPending = true;
      }
    }
    await refreshShownPage(any);
  } catch (e) {
    if (!e || !e.aborted) console.error('popout: refresh after another window wrote', e);
  }
}

async function refreshShownPage(any) {
  const tab = S.activeTab;
  if (!tab || tab === 'grid' || !$('popoutPlaceholder').hidden) return;
  if (tab === 'timeline') { if (any('tags', 'sources', 'notes')) await buildTimeline({ keepScroll: true }); }
  else if (tab === 'watchlist') { if (any('watchlist', 'tags', 'sources')) await showWatchlistTab(); }
  else if (tab === 'notes') { if (any('notes', 'sources')) loadRowNotes(); }
  else if (tab === 'dashboard') {
    if (any('dashboards')) await showDashboard(S.dashboardId);
    else markDashboardStale();
  } else if (tab.startsWith('plugin:')) {
    // What a return to the tab would do: onShow, which for the Pivot
    // re-reads the tables and re-runs a pivot filtered on tags.
    if (any('tags', 'sources', 'notes', 'variables')) await showPluginTab(tab.slice('plugin:'.length));
  }
}

/* ----------------------------------------------------------- messages */

function onMessage(e) {
  const m = e.data;
  if (!m || typeof m !== 'object' || m.family !== family() || m.from === me()) return;
  if (m.type === 'changed') { queueRefresh(m.kinds); return; }
  if (inPopout()) {
    if (m.from !== 'main') return;
    if (m.type === 'hello') {
      clearTimeout(ownerGone);
      ownerGone = null;
      send({ type: 'here', key: ownKey });
    } else if (m.type === 'leaving') {
      clearTimeout(ownerGone);
      ownerGone = setTimeout(ownerClosed, OWNER_GRACE_MS);
    } else if (m.type === 'dock-request' && m.key === ownKey) {
      dock();
    }
    return;
  }
  const key = m.key;
  if (m.type === 'here' && typeof key === 'string') {
    const was = popped.has(key);
    if (!was) popped.set(key, { win: null });
    // A main window reloaded with the page showing here while it was out:
    // the pop-out has the live copy, so this one stands down.
    if (pageShowing(key)) {
      if (key.startsWith('plugin:')) dropPluginTabMount(key.slice(7));
      showPlaceholder(key);
    }
    if (!was) paintPopped();
  } else if (m.type === 'gone') {
    returnPage(key, { delay: GONE_SETTLE_MS });
  } else if (m.type === 'dock') {
    returnPage(key);
    try { window.focus(); } catch { /* best effort */ }
  } else if (m.type === 'act' && Object.prototype.hasOwnProperty.call(ACTIONS, m.name)) {
    try { window.focus(); } catch { /* best effort */ }
    Promise.resolve(ACTIONS[m.name](...(Array.isArray(m.args) ? m.args : [])))
      .catch((err) => { if (!err || !err.aborted) toast(`Could not do that from the pop-out: ${(err && err.message) || err}`, 6000); });
  }
}

/* Called once by main.js, before boot, in every window. */
export function wirePopout() {
  if (inPopout()) document.documentElement.setAttribute('data-popout', '');
  if (typeof BroadcastChannel === 'undefined') return;
  channel = new BroadcastChannel(CHANNEL);
  channel.onmessage = onMessage;
  onMutation(onMutationSent);
  if (inPopout()) {
    $('btnPopoutDock').onclick = () => dock();
    watchOwner();
    window.addEventListener('pagehide', (e) => {
      if (e.persisted) return;
      savePlacement();
      if (docking) return;
      flushPageSaves(ownKey, { keepalive: true }).catch((err) => console.error(err));
      if (!following) send({ type: 'gone', key: ownKey });
    });
  } else {
    // A reloaded main window finds the pop-outs it already had.
    send({ type: 'hello' });
    window.addEventListener('pagehide', (e) => {
      if (!e.persisted) send({ type: 'leaving' });
    });
  }
}
