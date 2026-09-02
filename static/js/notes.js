/* Case notes — a free-form Markdown scratchpad for the investigation's
   narrative, distinct from per-row notes. Stored in the case file
   (Store.case_notes), so the story travels with the .db to whoever
   receives it. Its own page tab; edit/preview toggle; a tiny
   dependency-free Markdown renderer (airgap rule); debounced autosave.
   See docs/design/analysis-suite.md. */

import { $, api, debounce, el, post, toast } from './core.js';
import { showDashboard } from './dashboard.js';
import { recordTabVisit } from './tabhistory.js';
import { loadSqlTabs, showMainView, showSqlTab, syncTabChrome } from './sql.js';
import { openSource, sourceLabel, syncTabSelection } from './sources.js';
import { S } from './state.js';
import { dropdownMenu } from './ui.js';

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
    // In-app links: [label](winnow:table/12), winnow:sql/3, winnow:dashboard/2.
    // Rendered with a data attribute (no href navigation) — the preview's
    // click handler resolves them; the target set stays a validated
    // kind/id pair, so nothing user-typed reaches an executable sink.
    .replace(/\[([^\]]+)\]\(winnow:(table|sql|dashboard)\/(\d+)\)/g,
             '<a href="#" class="notes-link" data-winnow="$2/$3">$1</a>')
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

/* Follow a winnow: link from the preview — a note that says "see the
   4624 sweep" can now BE the navigation to it. */
function followWinnowLink(spec) {
  const [kind, idText] = spec.split('/');
  const id = Number(idText);
  if (kind === 'table') {
    if (S.sources.some((s) => s.id === id)) openSource(id);
    else toast('That table is no longer in this case');
  } else if (kind === 'sql') {
    S.sqlTabId = id;
    showSqlTab();
  } else if (kind === 'dashboard') {
    showDashboard(id);
  }
}

/* Insert [name](winnow:…) at the editor's cursor — links are meant to be
   picked from what exists, not hand-authored ids. */
async function insertNotesLink(anchor) {
  // The SQL tabs load lazily with their pane — fetch them here so a query
  // can be linked without having visited SQL first this session.
  if (!(S.sqlTabs || []).length) { try { await loadSqlTabs(); } catch { /* menu just omits queries */ } }
  const items = [];
  for (const s of S.sources.filter((x) => !x.error)) {
    items.push({ label: `Table: ${sourceLabel(s)}`,
                 onclick: () => insertAtCursor(`[${sourceLabel(s)}](winnow:table/${s.id})`) });
  }
  if ((S.sqlTabs || []).length) items.push('-');
  for (const t of S.sqlTabs || []) {
    items.push({ label: `Query: ${t.name}`,
                 onclick: () => insertAtCursor(`[${t.name}](winnow:sql/${t.id})`) });
  }
  if ((S.dashboards || []).length) items.push('-');
  for (const d of S.dashboards || []) {
    items.push({ label: `Dashboard: ${d.name}`,
                 onclick: () => insertAtCursor(`[${d.name}](winnow:dashboard/${d.id})`) });
  }
  if (!items.length) { toast('Nothing to link to yet'); return; }
  dropdownMenu(anchor, items);
}

function insertAtCursor(text) {
  const ed = $('notesEditor');
  const at = ed.selectionStart ?? ed.value.length;
  ed.value = ed.value.slice(0, at) + text + ed.value.slice(ed.selectionEnd ?? at);
  ed.selectionStart = ed.selectionEnd = at + text.length;
  ed.focus();
  ed.dispatchEvent(new Event('input'));   // autosave sees it like typing
}

export function wireNotes() {
  $('tabNotes').onclick = showNotesTab;
  $('notesEditor').oninput = () => { $('notesSaved').textContent = 'Saving…'; save(); };
  $('btnNotesEdit').onclick = () => setMode(true);
  $('btnNotesPreview').onclick = () => setMode(false);
  $('btnNotesLink').onclick = () => insertNotesLink($('btnNotesLink'));
  // Delegated — the preview re-renders wholesale on every mode switch.
  $('notesPreview').addEventListener('click', (e) => {
    const a = e.target.closest('a.notes-link');
    if (!a) return;
    e.preventDefault();
    followWinnowLink(a.dataset.winnow);
  });
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
      // Don't clobber text the analyst has already typed: the load is async,
      // so typing into a just-opened Notes tab can race ahead of it. Only
      // seed the editor from the saved body when it's still empty.
      if (!$('notesEditor').value) $('notesEditor').value = r.body || '';
      loaded = true;
    } catch { /* leave whatever's there */ }
  }
  setMode(true);
  setTimeout(() => $('notesEditor').focus(), 0);
}
