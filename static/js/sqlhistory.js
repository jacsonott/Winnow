/* The SQL pane's query history (History ▾): every query the pane has run
   in this case, newest first, one entry per distinct text. Stored in the
   case file's sql_history table (Store.record_sql_history), next to the
   query tabs and for the same reason.

   It exists because a query tab is a draft. Its text autosaves as it is
   edited, so the query behind the number an analyst wrote down an hour
   ago has usually been edited into the next idea since. The history keeps
   what actually ran.

   runSql records a run once it has settled and still belongs to its tab
   (recordSqlRun), so a superseded run (run again, its tab closed, another
   case opened) is not recorded; the run that replaced it is. Starters and
   a plugin's winnow.sqlPage.run go through runSql, so they are recorded
   the same way. Nothing here is cached: the panel asks the server each
   time it opens, which is one read of at most SQL_HISTORY_LIMIT short
   rows, and it means a pop-out running queries beside the main window
   never leaves either of them showing a stale list. */
import { $, IS_MAC, api, el, post, toast } from './core.js';
import { openedAgo } from './home.js';
import { runSql } from './plugins.js';
import { activeSqlTab, flushSqlTabSave, newSqlTab, sqlStarters } from './sql.js';
import { sqlAcClose } from './sqlassist.js';
import { S } from './state.js';
import { anchoredPanel, choiceDialog, confirmDialog } from './ui.js';

/* Store.SQL_HISTORY_LIMIT, for the footer's wording only; the server
   enforces it. */
export const SQL_HISTORY_LIMIT = 500;

/* Called by runSql with a run that has landed in its tab: a result, or an
   error. Best-effort and not awaited, like the tab autosave: the result is
   already on screen, and a history entry that failed to write is not
   worth a toast. Answers the entry (null when nothing was recorded), for
   anything that does want to wait. */
export function recordSqlRun(tabId, sql, r) {
  if (!r || !String(sql || '').trim()) return Promise.resolve(null);
  const tab = S.sqlTabs.find((t) => t.id === tabId);
  const ok = !r.error;
  const num = (v) => (ok && Number.isFinite(v) ? v : null);
  return post('/api/sql_history', {
    sql,
    tab_name: tab ? tab.name : null,
    row_count: num(r.total),
    elapsed_ms: num(r.elapsed_ms),
    stopped: ok ? r.stopped || null : null,
    error: ok ? null : String(r.error),
  }).catch(() => null);
}

const plural = (n, one, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`;
const pad2 = (n) => String(n).padStart(2, '0');

/* "2026-11-08 14:02", in this machine's time — the tooltip's exact
   counterpart to the list's "5 min ago". ran_at is UTC (see META_SCHEMA),
   and Date reads the Z. */
export function sqlHistoryStamp(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/* The entry's second line: where it was run, when, and how it went. */
function metaNodes(h) {
  const bits = [];
  if (h.tab_name) bits.push(h.tab_name);
  bits.push(openedAgo(h.ran_at));
  if (!h.error) {
    if (h.row_count != null) bits.push(plural(h.row_count, 'row') + (h.stopped ? ' (stopped)' : ''));
    if (h.elapsed_ms != null) bits.push(`${h.elapsed_ms.toLocaleString()} ms`);
  }
  if (h.runs > 1) bits.push(`run ${h.runs.toLocaleString()} times`);
  const nodes = [document.createTextNode(bits.join(' · '))];
  if (h.error) {
    // One line of it: an error can quote a whole statement back.
    nodes.push(document.createTextNode(' · '), el('span', 'sqlh-error', h.error.split('\n')[0]));
  }
  return nodes;
}

/* Every word typed has to appear somewhere in the query or its tab's name,
   in any order: "4624 logon" finds the query that says
   `EventId = '4624'` under a tab named Logons. */
export function sqlHistoryMatches(entries, q) {
  const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return entries;
  return entries.filter((h) => {
    const hay = `${h.sql}\n${h.tab_name || ''}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}

/* Opens an entry in a query tab of its own when `newTab` is set (its
   New tab button, or Ctrl/⌘+click), under the name of the tab it was run
   from when that name is free; otherwise in the current tab. Either way it must not cost the analyst a draft: the current tab's
   text is replaced without asking only under openStarter's rule (sql.js)
   plus one more kind of text, a query that is itself in the history,
   because the list still holds it. Anything else is asked about first,
   with a new tab offered beside replacing it. */
export async function openSqlHistoryEntry(h, { run = false, newTab = false, history = [] } = {}) {
  const tab = activeSqlTab();
  let fresh = newTab || !tab;
  if (!fresh) {
    const cur = $('sqlText').value.trim();
    const disposable = !cur
      || cur === h.sql
      || sqlStarters().some((x) => x.sql.trim() === cur)
      || history.some((x) => x.sql === cur);
    if (!disposable) {
      const pick = await choiceDialog(
        `The query in "${tab.name}" isn't in the history, so replacing it would lose it.`,
        [
          { label: 'Replace it', value: 'replace', danger: true },
          { label: 'Open in a new tab', value: 'new', primary: true },
        ],
        { focus: 'new' });
      if (!pick) return;
      fresh = pick === 'new';
    }
  }
  if (fresh) {
    const taken = new Set(S.sqlTabs.map((t) => t.name));
    await newSqlTab(h.tab_name && !taken.has(h.tab_name) ? h.tab_name : null, h.sql);
    // newSqlTab says so itself when it can't make the tab, and leaves the
    // draft's tab in the editor — which Run must not then run.
    if ($('sqlText').value !== h.sql) return;
  } else {
    $('sqlText').value = h.sql;
    // Through the editor's own input handler, so the tab record, the
    // autosave and the box's auto-grow all see it as they would typing.
    $('sqlText').dispatchEvent(new Event('input', { bubbles: true }));
    sqlAcClose();   // that event opens autocomplete on the query's last word
    await flushSqlTabSave();
    $('sqlText').focus();
  }
  if (run) await runSql();
}

/* History ▾. A search box over the list, the way the column picker and
   the value picker are: Enter opens the first match and Ctrl/⌘+Enter
   runs it, the editor's own Run key; the arrow keys walk the entries
   (ui.js onMenuKeydown, which moves between .menu-item buttons). */
export function openSqlHistory(anchorEl = $('btnSqlHistory')) {
  const panel = anchoredPanel(anchorEl, 'sql-history', (p, close) => {
    let entries = null;   // null until the list arrives
    let failed = '';
    const search = el('input', 'vp-search sqlh-search');
    search.type = 'search';
    search.placeholder = 'Find a query…';
    search.setAttribute('aria-label', 'Find a query in the history');
    const list = el('div', 'vp-list sqlh-list');
    const foot = el('div', 'sqlh-foot');

    const open = (h, opts) => {
      close();
      openSqlHistoryEntry(h, { ...opts, history: entries || [] })
        .catch((e) => toast('Could not open the query: ' + e.message));
    };
    const forget = async (h) => {
      try {
        await api(`/api/sql_history/${h.id}`, { method: 'DELETE' });
      } catch (e) {
        toast('Could not remove it: ' + e.message);
        return;
      }
      entries = entries.filter((x) => x.id !== h.id);
      paint();
      search.focus();
    };
    const clearAll = async () => {
      const n = entries.length;
      close();
      if (!(await confirmDialog(
        `Clear the query history? ${plural(n, 'query', 'queries')} will be deleted from the case file. Your query tabs are not affected.`,
        { danger: true, okLabel: 'Clear history' }))) return;
      try {
        await api('/api/sql_history', { method: 'DELETE' });
        toast('Query history cleared');
      } catch (e) {
        toast('Could not clear the history: ' + e.message);
      }
    };

    const row = (h) => {
      const r = el('div', 'menu-item-row sqlh-row');
      r.dataset.id = h.id;
      const item = el('button', 'menu-item sqlh-item');
      const meta = el('span', 'sqlh-meta');
      meta.append(...metaNodes(h));
      item.append(el('span', 'sqlh-sql', h.sql.replace(/\s+/g, ' ')), meta);
      // The query itself, so a click is never a surprise, and the exact
      // time the list rounds to "2 h ago".
      item.title = `${h.sql}\n\nLast run ${sqlHistoryStamp(h.ran_at)}${h.tab_name ? ` in ${h.tab_name}` : ''}`
        + ` — click to open it, ${IS_MAC ? '⌘' : 'Ctrl+'}click to open it in a new tab`;
      item.onclick = (e) => open(h, { newTab: e.ctrlKey || e.metaKey });
      item.onkeydown = (e) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); open(h, { run: true }); }
      };
      const tab = el('button', 'menu-item-action sqlh-newtab', 'New tab');
      tab.title = 'Open this query in a query tab of its own';
      tab.onclick = (e) => { e.stopPropagation(); open(h, { newTab: true }); };
      const run = el('button', 'menu-item-action sqlh-run', 'Run');
      run.title = 'Open this query and run it';
      run.onclick = (e) => { e.stopPropagation(); open(h, { run: true }); };
      const rm = el('button', 'menu-item-action sqlh-rm', '✕');
      rm.title = 'Remove this query from the history';
      rm.setAttribute('aria-label', 'Remove this query from the history');
      rm.onclick = (e) => { e.stopPropagation(); forget(h); };
      r.append(item, tab, run, rm);
      return r;
    };

    const paint = () => {
      list.replaceChildren();
      foot.replaceChildren();
      if (failed) { list.append(el('div', 'vp-note', `Could not load the history: ${failed}`)); return; }
      if (!entries) { list.append(el('div', 'vp-note', 'Loading…')); return; }
      if (!entries.length) {
        list.append(el('div', 'vp-note', 'Nothing has been run in this case yet. Every query you run is kept here.'));
        return;
      }
      const hits = sqlHistoryMatches(entries, search.value);
      for (const h of hits) list.append(row(h));
      if (!hits.length) list.append(el('div', 'vp-note', 'No query matches that.'));
      const count = el('span', 'sqlh-count', hits.length === entries.length
        ? plural(entries.length, 'query', 'queries')
        : `${hits.length.toLocaleString()} of ${plural(entries.length, 'query', 'queries')}`);
      count.title = `Kept in the case file, newest first. The ${SQL_HISTORY_LIMIT} most recently run are kept.`;
      const clear = el('button', 'btn ghost sqlh-clear', 'Clear history…');
      clear.title = 'Delete every query in the history from the case file';
      clear.onclick = clearAll;
      foot.append(count, clear);
    };

    search.oninput = paint;
    search.onkeydown = (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const first = entries && sqlHistoryMatches(entries, search.value)[0];
      if (first) open(first, { run: e.ctrlKey || e.metaKey });
    };
    p.append(search, list, foot);
    paint();
    api('/api/sql_history')
      .then((rows) => { entries = rows; })
      .catch((e) => { failed = e.message; })
      .then(() => { if (p.isConnected) paint(); });
    setTimeout(() => search.focus(), 0);
  });
  // anchoredPanel puts it under the button; this keeps it there but as
  // wide as the page.
  if (panel) fitToPage(panel);
  return panel;
}

/* As wide as the SQL page (its main column, so not over a plugin's side
   panel), less a margin each side, and lined up with it rather than with
   the button. Widening can only make the panel shorter (fewer queries
   wrap onto their second line), so where anchoredPanel put it vertically
   still fits. */
function fitToPage(p) {
  const page = document.querySelector('#sqlview .page-main');
  if (!page || !page.offsetWidth) return;
  const r = page.getBoundingClientRect();
  const m = 8;
  p.style.width = `${Math.max(320, r.width - 2 * m)}px`;
  p.style.left = `${Math.max(m, r.left + m)}px`;
}

/* DOM wiring for this module, called once by main.js. */
export function wireSqlHistory() {
  $('btnSqlHistory').onclick = () => openSqlHistory($('btnSqlHistory'));
}
