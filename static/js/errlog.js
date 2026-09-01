/* The in-app error log. Server-side warnings/errors (plugin load failures,
   unhandled request errors) used to go only to the terminal Winnow was
   started from — which an analyst rarely has in view. record_log (server.py)
   captures them into a bounded ring; this shows it from the Case menu, with
   a subtle dot on the Case button when unseen entries have arrived. */

import { $, api, el } from './core.js';
import { modal } from './ui.js';

const SEEN_KEY = 'winnow.log.seen';

/* Poll the log's high-water sequence and light the Case button's dot when
   entries newer than the last-viewed one exist. */
export async function refreshLogBadge() {
  let data;
  try { data = await api('/api/log'); } catch { return; }
  const seen = Number(localStorage.getItem(SEEN_KEY) || 0);
  const unseen = (data.entries || []).some((e) => e.seq > seen);
  const btn = $('btnCase');
  if (btn) btn.classList.toggle('has-errors', unseen);
}

export async function openErrorLog() {
  let data;
  try { data = await api('/api/log'); } catch { data = { entries: [], seq: 0 }; }
  // Opening the log is "I've seen these" — clear the dot and remember up to
  // the current high-water mark.
  localStorage.setItem(SEEN_KEY, String(data.seq || 0));
  $('btnCase')?.classList.remove('has-errors');
  modal('Error log', (b) => {
    b.append(el('p', 'fb-help',
      'Server-side warnings and errors this session — plugin load failures, request errors and the '
      + 'like. These also print to the terminal Winnow was started from.'));
    if (!(data.entries || []).length) {
      b.append(el('div', 'note-status', 'Nothing logged this session.'));
      return;
    }
    const list = el('div', 'errlog-list');
    for (const e of [...data.entries].reverse()) {   // newest first
      const row = el('div', 'errlog-row errlog-' + (e.level || 'info'));
      row.append(
        el('span', 'errlog-ts', e.ts || ''),
        el('span', 'errlog-lvl', (e.level || 'info').toUpperCase()),
        el('span', 'errlog-msg', e.message || ''),
      );
      list.append(row);
    }
    b.append(list);
  }, { wide: true });
}

/* Light poll started at boot — errors are rare, so a 20s cadence is plenty
   to surface "something went wrong" without a chattier loop. */
export function startLogBadgePoll() {
  refreshLogBadge();
  setInterval(refreshLogBadge, 20000);
}
