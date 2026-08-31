/* Recently-visited page tabs, navigable with the mouse's back/forward
   buttons — the same muscle memory as a browser, applied to the tab
   strip. A visit stack with a cursor, not a ring: going back and then
   opening something new truncates the forward side, exactly like
   browser history.

   Entries are { kind: 'source', id } for table/merge tabs and
   { kind: 'page', key } for SQL/Timeline/plugin tabs. Replay skips
   entries whose target no longer exists (a dropped source, a disabled
   plugin) rather than dying on them. */

import { showPluginTab } from './plugins.js';
import { openSource } from './sources.js';
import { showSqlTab, showTimelineTab } from './sql.js';
import { S } from './state.js';

let navigating = false;   // a replayed visit must not re-record itself

export function recordTabVisit(entry) {
  if (navigating) return;
  const cur = S.tabHistory[S.tabHistoryPos];
  if (cur && cur.kind === entry.kind && cur.id === entry.id && cur.key === entry.key) return;
  S.tabHistory.splice(S.tabHistoryPos + 1);   // new branch: drop the forward side
  S.tabHistory.push(entry);
  if (S.tabHistory.length > 100) S.tabHistory.shift();
  S.tabHistoryPos = S.tabHistory.length - 1;
}

export function clearTabHistory() {
  S.tabHistory = [];
  S.tabHistoryPos = -1;
}

function replay(entry) {
  if (entry.kind === 'source') {
    if (!S.sources.some((s) => s.id === entry.id)) return false;
    openSource(entry.id);
    return true;
  }
  if (entry.key === 'sql') { showSqlTab(); return true; }
  if (entry.key === 'timeline') { showTimelineTab(); return true; }
  if (S.pluginTabs && S.pluginTabs.some((t) => t.id === entry.key)) {
    showPluginTab(entry.key);
    return true;
  }
  return false;
}

export function tabHistoryGo(delta) {
  let pos = S.tabHistoryPos;
  while (true) {
    pos += delta;
    if (pos < 0 || pos >= S.tabHistory.length) return;
    navigating = true;
    try {
      if (replay(S.tabHistory[pos])) { S.tabHistoryPos = pos; return; }
    } finally {
      navigating = false;
    }
    // Target gone (dropped source, disabled plugin): keep walking past it.
  }
}

/* Wired from main.js: 3 is the browser-back thumb button, 4 is forward.
   The SPA has no history entries, so the browser's own back would tear
   the analyst out of the app (or do nothing) — either way the buttons
   were dead weight before this. */
export function onMouseNav(e) {
  if (e.button === 3) { e.preventDefault(); tabHistoryGo(-1); }
  else if (e.button === 4) { e.preventDefault(); tabHistoryGo(1); }
}
