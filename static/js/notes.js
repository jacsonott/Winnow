/* Case notes — a free-form Markdown scratchpad for the investigation's
   narrative, distinct from per-row notes. Stored in the case file
   (Store.case_notes), so the story travels with the .db to whoever
   receives it. Its own page tab; edit/preview toggle; a tiny
   dependency-free Markdown renderer (airgap rule); debounced autosave.
   See docs/design/analysis-suite.md. */

import { $, api, debounce, el, post, toast } from './core.js';
import { recordTabVisit } from './tabhistory.js';
import { showMainView, syncTabChrome } from './sql.js';
import { syncTabSelection } from './sources.js';
import { S } from './state.js';

let loaded = false;   // whether this case's notes have been fetched into the editor

/* Minimal Markdown → HTML. Escapes first, then a handful of inline/block
   rules — headings, bold, italic, inline code, fenced code, links, and
   unordered lists. Deliberately small: no library ships to an airgapped
   box. Anything unrecognised renders as plain text. */
export function renderMarkdown(src) {
  const esc = (t) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const out = [];
  const lines = (src || '').split('\n');
  let inList = false, inCode = false;
  const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
  for (const line of lines) {
    if (/^```/.test(line)) {
      if (inCode) { out.push('</code></pre>'); inCode = false; }
      else { closeList(); out.push('<pre><code>'); inCode = true; }
      continue;
    }
    if (inCode) { out.push(esc(line)); continue; }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) { closeList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
    const li = /^\s*[-*]\s+(.*)$/.exec(line);
    if (li) { if (!inList) { out.push('<ul>'); inList = true; } out.push(`<li>${inline(li[1])}</li>`); continue; }
    if (!line.trim()) { closeList(); continue; }
    closeList();
    out.push(`<p>${inline(line)}</p>`);
  }
  if (inCode) out.push('</code></pre>');
  closeList();
  return out.join('\n');
}

const save = debounce(async () => {
  const status = $('notesSaved');
  try {
    await post('/api/case/notes', { body: $('notesEditor').value });
    if (status) { status.textContent = 'Saved'; setTimeout(() => { if (status.textContent === 'Saved') status.textContent = ''; }, 1500); }
  } catch (e) { if (status) status.textContent = 'Save failed'; }
}, 600);

function setMode(editing) {
  $('notesEditor').hidden = !editing;
  $('notesPreview').hidden = editing;
  $('btnNotesEdit').setAttribute('aria-pressed', String(editing));
  $('btnNotesPreview').setAttribute('aria-pressed', String(!editing));
  $('btnNotesEdit').className = editing ? 'btn' : 'btn ghost';
  $('btnNotesPreview').className = editing ? 'btn ghost' : 'btn';
  if (!editing) $('notesPreview').innerHTML = renderMarkdown($('notesEditor').value);
}

export function wireNotes() {
  $('tabNotes').onclick = showNotesTab;
  $('notesEditor').oninput = () => { $('notesSaved').textContent = 'Saving…'; save(); };
  $('btnNotesEdit').onclick = () => setMode(true);
  $('btnNotesPreview').onclick = () => setMode(false);
}

// A case switch invalidates the loaded body; refetch on next open.
export function resetNotes() { loaded = false; }

export async function showNotesTab() {
  recordTabVisit({ kind: 'page', key: 'notes' });
  S.activeTab = 'notes';
  showMainView('notesview');
  syncTabSelection();
  syncTabChrome();
  if (!loaded) {
    try {
      const r = await api('/api/case/notes');
      $('notesEditor').value = r.body || '';
      loaded = true;
    } catch { /* leave whatever's there */ }
  }
  setMode(true);
  setTimeout(() => $('notesEditor').focus(), 0);
}
