/* The in-app log. Server-side errors (plugin load failures, unhandled
   request errors) used to go only to the terminal Winnow was started from
   — which an analyst rarely has in view. winnow/log.py keeps a bounded
   ring; imports and exports write to it too now, so this is the place to
   ask "did that finish, and how long did it take". Shown from the Case
   menu, with a dot on the Case button when an ERROR has arrived that
   hasn't been looked at — info entries never light it, or the dot would
   mean nothing by the third import. */

import { $, api, el, post } from './core.js';
import { modal } from './ui.js';

const SEEN_KEY = 'winnow.log.seen';

/* Poll the log and light the Case button's dot when an error newer than
   the last-viewed one exists. Keyed to error_seq, not seq: an import
   finishing is news, not a problem. */
/* The mark is "<boot>:<error_seq>": seq restarts at 0 with every server
   process, so a bare number from a previous run would hide this run's
   first errors (a plugin failing at startup lands at seq 1). A mark from
   another boot counts as nothing seen. */
function seenErrorSeq(boot) {
  const [b, n] = String(localStorage.getItem(SEEN_KEY) || '').split(':');
  return String(b) === String(boot) ? Number(n || 0) : 0;
}
function markSeen(boot, errorSeq) {
  localStorage.setItem(SEEN_KEY, `${boot}:${errorSeq || 0}`);
}

export async function refreshLogBadge() {
  let m;
  try { m = await api('/api/log/marks'); } catch { return; }   // three numbers, not the ring
  const unseen = (m.error_seq || 0) > seenErrorSeq(m.boot);
  const btn = $('btnCase');
  if (btn) btn.classList.toggle('has-errors', unseen);
}

/* The browser's side of the log — a poll whose fetch rejected, a response
   that wouldn't parse. The server can't see those (they never reach a
   route), and they're what a "the progress bar stopped" report is made
   of. Rate-limited per message so a dead server doesn't queue hundreds
   of identical lines for when it comes back; swallows everything, since
   logging must never be the thing that fails. */
const recentClientLogs = new Map();
export function clientLog(level, message) {
  const now = Date.now();
  const last = recentClientLogs.get(message) || 0;
  if (now - last < 30000) return;
  recentClientLogs.set(message, now);
  try { post('/api/log/client', { level, message }).catch(() => {}); } catch { /* nothing to do */ }
}

export async function openLog() {
  let data;
  try { data = await api('/api/log'); } catch { data = { entries: [], seq: 0, error_seq: 0 }; }
  // Opening the log is "I've seen these" — clear the dot and remember the
  // latest error we showed, keyed to this server process. (If the ring
  // has wrapped past that error, there is nothing left to show for it;
  // the badge clears and the All view is what's left.)
  markSeen(data.boot, data.error_seq);
  $('btnCase')?.classList.remove('has-errors');
  const entries = [...(data.entries || [])].reverse();   // newest first
  const hasErrors = entries.some((e) => e.level === 'error');
  const state = { mode: hasErrors ? 'errors' : 'all', q: '' };
  modal('Log', (b) => {
    b.append(el('p', 'fb-help',
      'This session, newest first: imports and exports as they start and finish, plus server-side '
      + 'warnings and errors. Everything here also prints to the terminal Winnow was started from.'));
    const bar = el('div', 'errlog-bar');
    const seg = el('div', 'vp-seg');   // the app's one segmented control, contrast already fixed
    const segBtns = {};
    for (const [key, label] of [['errors', 'Errors'], ['all', 'All']]) {
      const btn = el('button', 'btn ghost', label);
      btn.onclick = () => { state.mode = key; paint(); };
      segBtns[key] = btn;
      seg.append(btn);
    }
    const search = el('input', 'panel-search errlog-search');
    search.type = 'search';
    search.placeholder = 'Filter…';
    search.autocomplete = 'off';
    search.oninput = () => { state.q = search.value.trim().toLowerCase(); paint(); };
    bar.append(seg, search);
    b.append(bar);
    const list = el('div', 'errlog-list');
    b.append(list);

    function paint() {
      for (const [key, btn] of Object.entries(segBtns)) btn.setAttribute('aria-pressed', String(state.mode === key));
      list.replaceChildren();
      const shown = entries.filter((e) =>
        (state.mode === 'all' || e.level === 'error')
        && (!state.q || String(e.message || '').toLowerCase().includes(state.q)));
      if (!shown.length) {
        list.append(el('div', 'note-status', entries.length
          ? (state.q ? 'Nothing matches that filter.' : 'No errors this session.')
          : 'Nothing logged this session.'));
        return;
      }
      for (const e of shown) {
        const row = el('div', 'errlog-row errlog-' + (e.level || 'info'));
        row.append(
          el('span', 'errlog-ts', e.ts || ''),
          el('span', 'errlog-lvl', (e.level || 'info').toUpperCase()),
          el('span', 'errlog-msg', e.message || ''),
        );
        list.append(row);
      }
    }
    paint();
  }, { wide: true });
}

/* The old name, for anything still calling it. */
export const openErrorLog = openLog;

/* Light poll started at boot — errors are rare, so a 20s cadence is plenty
   to surface "something went wrong" without a chattier loop. */
export function startLogBadgePoll() {
  refreshLogBadge();
  setInterval(refreshLogBadge, 20000);
}
