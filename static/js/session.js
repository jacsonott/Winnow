/* Saving and loading session files.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, api, el, post, setBusy, toast } from './core.js';
import { loadSources } from './sources.js';
import { confirmDialog, modal, promptDialog } from './ui.js';

export function openSessionManager() {
  modal('Session', (b) => {
    b.append(el('p', null,
      "A session captures every open source's tags, notes and layout. Named saves live in a sessions/ "
      + 'folder next to the case file; the download/upload flow at the bottom is for handing a session '
      + 'to another analyst on another machine.'));

    b.append(el('h4', null, 'Recent sessions'));
    const recentList = el('div', 'session-list');
    b.append(recentList);

    async function refreshRecent() {
      recentList.replaceChildren(el('div', 'note-status', 'Loading…'));
      try {
        const sessions = await api('/api/sessions');
        recentList.replaceChildren();
        if (!sessions.length) { recentList.append(el('div', 'note-status', 'No saved sessions yet.')); return; }
        for (const s of sessions) {
          const row = el('div', 'row-actions session-row');
          row.append(
            el('span', 'session-name', s.name),
            el('span', 'count', `${s.source_count} source${s.source_count === 1 ? '' : 's'} · ${s.saved_at || ''}`),
          );
          const openBtn = el('button', 'btn ghost', 'Load');
          openBtn.onclick = async () => {
            setBusy(true);
            let res;
            try {
              res = await api(`/api/sessions/${encodeURIComponent(s.name)}/load`, { method: 'POST' });
              await loadSources();
            } finally {
              setBusy(false);
            }
            (res.warnings || []).forEach((w) => toast(w, 6000));
            $('modal').hidden = true;
            toast(`Loaded "${s.name}" · ${res.tags_applied.toLocaleString()} tag assignments across ${res.sources_restored} source(s)`);
          };
          const del = el('button', 'btn ghost', '✕');
          del.title = 'Delete this saved session';
          del.onclick = async () => {
            if (!(await confirmDialog(`Delete saved session "${s.name}"?`, { danger: true, okLabel: 'Delete' }))) return;
            await api(`/api/sessions/${encodeURIComponent(s.name)}`, { method: 'DELETE' });
            refreshRecent();
          };
          row.append(openBtn, del);
          recentList.append(row);
        }
      } catch (e) {
        recentList.replaceChildren(el('div', 'note-status', 'Could not load sessions: ' + e.message));
      }
    }
    refreshRecent();

    const saveActs = el('div', 'row-actions');
    const saveAs = el('button', 'btn', 'Save current case as…');
    saveAs.onclick = async () => {
      const name = await promptDialog('Session name:');
      if (!name || !name.trim()) return;
      setBusy(true);
      try { await post('/api/sessions', { name: name.trim() }); }
      finally { setBusy(false); }
      toast(`Saved session "${name.trim()}"`);
      refreshRecent();
    };
    saveActs.append(saveAs);
    b.append(saveActs);

    b.append(el('h4', null, 'Share with another analyst'));
    const shareActs = el('div', 'row-actions');
    const save = el('button', 'btn ghost', 'Download session file');
    save.onclick = () => { window.location = '/api/case_session'; };
    const loadLabel = el('label', 'btn ghost', 'Load session file');
    const input = el('input');
    input.type = 'file';
    input.accept = '.json';
    input.hidden = true;
    input.onchange = async () => {
      const fd = new FormData();
      fd.append('file', input.files[0]);
      fd.append('merge', 'true');
      setBusy(true);
      let res;
      try {
        res = await api('/api/case_session', { method: 'POST', body: fd });
        await loadSources();
      } finally {
        setBusy(false);
      }
      (res.warnings || []).forEach((w) => toast(w, 6000));
      $('modal').hidden = true;
      toast(`Applied ${res.tags_applied.toLocaleString()} tag assignments across ${res.sources_restored} source(s)`);
    };
    loadLabel.append(input);
    shareActs.append(save, loadLabel);
    b.append(shareActs);
  }, { wide: true });
}
