/* The Tables manager — row counts, indexes, dropping a source, compacting.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { openTableMenu } from './timeframe.js';
import { $, api, el, post, setBusy, toast } from './core.js';
import { writeClipboardText } from './grouping.js';
import { sqlSchemaForLLM } from './plugins.js';
import { editSourceNickname, loadSources, sourceLabel, sourceTitle } from './sources.js';
import { S } from './state.js';
import { markModalAction, confirmDialog, modal } from './ui.js';

/* Every source/merge in the case, open or not — the counterpart to the tab
   strip's now-nondestructive ✕. Open/Close just flips visibility; Remove is
   the one place the old hard-delete-on-close behavior still lives. Also
   folds in search index status (Contains/Advanced-mode search uses a
   per-table trigram substring index built in the background — see
   CLAUDE.md's "Things that bite") as a column rather than a separate
   modal, since both are "state of every table in this case" views —
   background-polled the same way the old standalone index-status modal
   was, refetching S.sources on a plain timer rather than going through
   loadSources() (which resets the open source's filters/search/sort as a
   side effect of re-selecting its tab — fine for a real navigation, not
   something a background status poll should ever trigger). */
export let tablesModalPoll = null;

export async function refreshSourcesQuietly() {
  const [sources, merges] = await Promise.all([api('/api/sources'), api('/api/merges')]);
  S.sources = [...sources, ...merges];
}

export function indexStatusFor(s) {
  if (s.has_fts) return { text: '✓ Ready', cls: 'ready' };
  if (s.fts_building) return { text: '⏳ Building…', cls: 'building' };
  return { text: 'Not started', cls: 'idle' };
}

export function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

/* VACUUM, behind a confirm. Nothing else in the app ever returns freed
   pages to the OS — dropping a table, or the startup janitor clearing out
   a stale FTS index (which on a fat-trigram case file is most of its bulk),
   frees them to SQLite's own freelist, where they stay reserved for this
   case file forever. That's the right default, but after a big cleanup it
   can be tens of GB parked on disk with no way to ask for it back. The SQL
   pane deliberately refuses VACUUM (see run_sql), so this button is it. */
export async function compactCaseFile() {
  const ok = await confirmDialog(
    'Compact this case file?\n\n'
    + 'This rewrites the whole file to return space freed by removed tables and '
    + 'indexes to the operating system. On a large case it can take several minutes, '
    + 'during which the app will be unresponsive, and it needs as much free disk '
    + 'space as the case file currently uses.',
    { okLabel: 'Compact' },
  );
  if (!ok) return;
  setBusy(true);
  let res;
  try { res = await post('/api/case/compact', {}); }
  catch (e) { toast('Compact failed: ' + e.message, 6000); return; }
  finally { setBusy(false); }
  // before/after_bytes are already whole-footprint (main + -wal) — the
  // store owns that definition so this can't drift from reclaimed_bytes.
  // A checkpoint the readers wouldn't let finish leaves the freed bytes
  // in the WAL until a later passive one collects them; reclaimed_bytes
  // already counts them as not-reclaimed, so the toast just says why.
  const pending = res.wal_checkpointed === false && res.wal_pending_bytes > 0
    ? ` · ${fmtBytes(res.wal_pending_bytes)} still in the write-ahead log (a read was holding it open); it frees up on its own shortly`
    : '';
  toast((res.reclaimed_bytes > 0
    ? `Compacted: ${fmtBytes(res.before_bytes)} → ${fmtBytes(res.after_bytes)} on disk, reclaimed ${fmtBytes(res.reclaimed_bytes)}`
    : `Compacted — nothing to reclaim (${fmtBytes(res.after_bytes)} on disk)`) + pending, 8000);
}

export function openTablesManager() {
  markModalAction('openTables');
  modal('Tables', (b) => {
    b.dataset.kind = 'tables';
    b.append(el('p', null,
      'Every table in this case. Closing a tab from the header only hides it here — '
      + 'reopen it below, or use Remove to delete it (and its tags/notes) for good. '
      + 'Contains/Advanced search uses a per-table substring index built in the background '
      + '(shown below) — a table without one yet still searches correctly, just via a slower full scan. '
      + 'Filtering, grouping or opening the value picker on a column also builds a small index for '
      + 'that column; those are listed per table so they can be dropped if they add up.'));

    /* source_id -> [{column, building}]. Fetched once per modal open rather
       than joined onto /api/sources: it's one sqlite_master read per table
       and this modal already re-polls /api/sources every 1.5s for the FTS
       build status, which would turn into an N+1 on every tick. */
    const indexesBySource = new Map();
    async function refreshIndexes() {
      const real = S.sources.filter((s) => !s.is_merge && !s.error);
      const results = await Promise.all(real.map((s) =>
        api(`/api/column_indexes?source_id=${s.id}`).catch(() => [])));
      real.forEach((s, i) => indexesBySource.set(s.id, results[i]));
    }

    const acts = el('div', 'row-actions');
    const openAllTagged = el('button', 'btn ghost', 'Open all tagged');
    openAllTagged.onclick = async () => {
      const targets = S.sources.filter((s) => !s.is_merge && !s.error && !s.is_open && s.tagged_row_count > 0);
      if (!targets.length) { toast('No closed tables have tagged rows'); return; }
      setBusy(true);
      try {
        for (const s of targets) await post(`/api/source/${s.id}/open`, { open: true });
      } finally { setBusy(false); }
      await loadSources();
      openTablesManager();
    };
    const copySchema = el('button', 'btn ghost', 'Copy table definitions');
    copySchema.title = 'Copy every table\'s columns as SQL — paste into an LLM prompt to help write a SQL pane query';
    copySchema.onclick = () => {
      const real = S.sources.filter((s) => !s.is_merge && !s.error);
      if (!real.length) { toast('No tables to copy'); return; }
      writeClipboardText(Promise.resolve(sqlSchemaForLLM()), `Copied ${real.length} table definition${real.length === 1 ? '' : 's'}`);
    };
    const compact = el('button', 'btn ghost', 'Compact case file…');
    compact.title = 'VACUUM — return space freed by removed tables and indexes to the operating system';
    compact.onclick = async () => { await compactCaseFile(); };
    acts.append(openAllTagged, copySchema, compact);
    b.append(acts);

    const list = el('div', 'session-list');
    b.append(list);

    function render() {
      list.replaceChildren();
      for (const s of S.sources) {
        const row = el('div', 'row-actions session-row');
        const nameSpan = el('span', 'session-name', (s.is_merge ? '⛓ ' : '') + sourceLabel(s) + (s.error ? ' ⚠' : ''));
        nameSpan.title = sourceTitle(s);
        row.append(nameSpan);
        row.append(el('span', 'count', s.error
          ? s.error
          : `${s.row_count.toLocaleString()} rows · ${s.tagged_row_count.toLocaleString()} tagged · ${s.note_count.toLocaleString()} notes`));
        if (!s.error) {
          const status = indexStatusFor(s);
          row.append(el('span', 'index-status index-status-' + status.cls, status.text));
        }
        const toggle = el('button', 'btn ghost', s.is_open ? 'Close' : 'Open');
        toggle.onclick = async () => {
          setBusy(true);
          try { await post(`/api/source/${s.id}/open`, { open: !s.is_open }); }
          finally { setBusy(false); }
          if (s.is_open && S.sourceId === s.id) S.sourceId = null;
          await loadSources();
          openTablesManager();
        };
        const menuBtn = el('button', 'btn ghost', 'Settings…');
        menuBtn.title = 'The table menu — columns, pinning, exports, everything per-table';
        menuBtn.onclick = () => openTableMenu(s.id);   // opens the table itself first if it was closed
        if (!s.error) row.append(menuBtn);
        const nick = el('button', 'btn ghost', 'Nickname…');
        nick.title = s.is_merge ? 'Rename this merge' : 'A display name shown in place of the file name — clear it to go back';
        nick.onclick = async () => {
          if (!(await editSourceNickname(s))) return;
          openTablesManager();
        };
        const del = el('button', 'btn ghost', 'Remove…');
        del.onclick = async () => {
          const warn = s.is_merge
            ? `Delete merge "${sourceLabel(s)}"? The underlying sources are untouched.`
            : `Remove ${sourceLabel(s)} from this case? Tags and notes for it are deleted too.`;
          if (!(await confirmDialog(warn, { danger: true, okLabel: 'Remove' }))) return;
          if (s.is_merge) {
            await api(`/api/merges/${-s.id}`, { method: 'DELETE' });
          } else {
            await api(`/api/source/${s.id}`, { method: 'DELETE' });
            S.viewCache.delete(s.id); // SQLite can reuse a deleted source's row id — don't let a stale cached view leak onto it
          }
          if (S.sourceId === s.id) S.sourceId = null;
          await loadSources();
          openTablesManager();
        };
        row.append(toggle, nick, del);
        list.append(row);
        const idxs = indexesBySource.get(s.id) || [];
        if (idxs.length) list.append(columnIndexRow(s, idxs));
      }
    }

    /* One line per table that has auto-created filter indexes, with a drop
       per column. They're created silently and never expire, so on a case
       where the same headers get imported and filtered repeatedly they
       accumulate unseen; dropping one costs nothing but the next filter
       rebuilding it. */
    function columnIndexRow(s, idxs) {
      const wrap = el('div', 'row-actions source-indexes');
      wrap.append(el('span', 'fb-help', 'Filter indexes:'));
      for (const ix of idxs) {
        const chip = el('span', 'index-chip' + (ix.building ? ' building' : ''));
        chip.append(el('span', null, ix.column + (ix.building ? ' (building…)' : '')));
        if (!ix.building) {
          const drop = el('button', 'index-chip-drop', '✕');
          drop.title = `Drop the index on "${ix.column}" — searches and filters still work, just by scanning`;
          drop.onclick = async () => {
            setBusy(true);
            try {
              await api(`/api/column_indexes?source_id=${s.id}&column=${encodeURIComponent(ix.column)}`,
                        { method: 'DELETE' });
            } catch (e) { toast('Could not drop index: ' + e.message, 4000); return; }
            finally { setBusy(false); }
            await refreshIndexes();
            render();
            toast(`Dropped the filter index on "${ix.column}"`);
          };
          chip.append(drop);
        }
        wrap.append(chip);
      }
      return wrap;
    }

    render();
    refreshIndexes().then(render).catch(() => {});

    if (tablesModalPoll) clearInterval(tablesModalPoll);
    tablesModalPoll = setInterval(async () => {
      if ($('modal').hidden || $('modalBody').dataset.kind !== 'tables') {
        clearInterval(tablesModalPoll);
        tablesModalPoll = null;
        return;
      }
      try {
        await refreshSourcesQuietly();
        // Only re-poll the index list while one is mid-build — otherwise
        // this tick would be an N+1 (one query per table) every 1.5s just
        // to re-read a list that only changes when the analyst acts.
        if ([...indexesBySource.values()].some((l) => l.some((ix) => ix.building))) await refreshIndexes();
      } catch {
        clearInterval(tablesModalPoll);
        tablesModalPoll = null;
        return;
      }
      render();
    }, 1500);
  }, { wide: true });
}
