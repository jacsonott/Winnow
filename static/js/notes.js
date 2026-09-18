/* Case notes — a free-form Markdown scratchpad for the investigation's
   narrative, distinct from per-row notes. Stored in the case file
   (Store.case_notes), so the story travels with the .db to whoever
   receives it. Its own page tab; the editor and a live preview side by
   side, split by a draggable divider (Edit / Preview collapse the other
   pane; only the divider position is remembered, per browser, under
   winnow.notes); a tiny dependency-free Markdown renderer (airgap rule);
   debounced autosave. See docs/design/analysis-suite.md. */

import { $, api, debounce, el, post, toast } from './core.js';
import { showDashboard } from './dashboard.js';
import { recordTabVisit } from './tabhistory.js';
import { loadSqlTabs, showMainView, showSqlTab, syncTabChrome } from './sql.js';
import { openSource, sourceLabel, syncTabSelection } from './sources.js';
import { S } from './state.js';
import { dropdownMenu } from './ui.js';

let loaded = false;   // whether this case's notes have been fetched into the editor
let previewStale = true;   // the preview body doesn't reflect the editor (typed while it was hidden)

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
  let inList = false, inCode = false, inQuote = false;
  const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
  const closeQuote = () => { if (inQuote) { out.push('</blockquote>'); inQuote = false; } };
  // GitHub-style tables: a header row, a |---|---| separator, then rows.
  const cells = (l) => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
  const isSep = (l) => /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(l || '');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!inCode && /^\s*\|/.test(line) && isSep(lines[i + 1])) {
      closeList();
      out.push('<table><thead><tr>' + cells(line).map((c) => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>');
      i += 1;
      while (i + 1 < lines.length && /^\s*\|/.test(lines[i + 1])) {
        i += 1;
        out.push('<tr>' + cells(lines[i]).map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>');
      }
      out.push('</tbody></table>');
      continue;
    }
    if (/^```/.test(line)) {
      if (inCode) { out.push('</code></pre>'); inCode = false; }
      else { closeList(); out.push('<pre><code>'); inCode = true; }
      continue;
    }
    if (inCode) { out.push(esc(line)); continue; }
    // > quoted — consecutive lines make one blockquote, a paragraph each.
    const bq = /^\s*>\s?(.*)$/.exec(line);
    if (bq) { closeList(); if (!inQuote) { out.push('<blockquote>'); inQuote = true; } out.push(`<p>${inline(bq[1])}</p>`); continue; }
    closeQuote();
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
  closeQuote();
  return out.join('\n');
}

const save = debounce(async () => {
  const status = $('notesSaved');
  try {
    await post('/api/case/notes', { body: $('notesEditor').value });
    if (status) { status.textContent = 'Saved'; setTimeout(() => { if (status.textContent === 'Saved') status.textContent = ''; }, 1500); }
  } catch (e) { if (status) status.textContent = 'Save failed'; }
}, 600);

/* ------------------------------------------------------- layout: split */

/* The divider position is a per-browser UI preference like winnow.sidebar
   and winnow.detail — localStorage, never workspace/ or the case file. A
   ratio of the row, not px: the row's width changes whenever the plugin
   side column opens or the window resizes, and a 50/50 that stays 50/50
   is what "remembered" should mean. The MODE (Edit / Split / Preview) is
   deliberately NOT stored: Notes always opens in Split, the parallel view
   the page is for, and a remembered preview-only mode would hide the
   editor from the next visit's first keystroke. */
export const NOTES_KEY = 'winnow.notes';
export const NOTES_SPLIT_DEFAULT = 0.5;
export const NOTES_SPLIT_MIN = 0.2;
export const NOTES_SPLIT_MAX = 0.8;
export const NOTES_PANE_MIN_PX = 220;   // below this a pane is unusable; beats the ratio clamp on a narrow row
export const NOTES_MODES = ['edit', 'split', 'preview'];

export function loadNotesPrefs() {
  let stored = {};
  try { stored = JSON.parse(localStorage.getItem(NOTES_KEY) || '{}') || {}; } catch { /* defaults below */ }
  const split = Number(stored.split);
  return { split: split >= NOTES_SPLIT_MIN && split <= NOTES_SPLIT_MAX ? split : NOTES_SPLIT_DEFAULT };
}

export function saveNotesPrefs(patch) {
  let cur = {};
  try { cur = JSON.parse(localStorage.getItem(NOTES_KEY) || '{}') || {}; } catch { /* start fresh */ }
  try { localStorage.setItem(NOTES_KEY, JSON.stringify({ ...cur, ...patch })); } catch { /* private mode */ }
}

/* 0.2–0.8 of the row, and never a pane narrower than NOTES_PANE_MIN_PX
   when the row is wide enough to honour that — the plugin side column
   (up to 70% of the section) plus the ratio clamp alone could squeeze
   the editor to a few characters on a laptop. `width` 0 (page hidden,
   nothing laid out yet) falls back to the ratio clamp alone. */
export function clampNotesSplit(ratio, width) {
  let lo = NOTES_SPLIT_MIN, hi = NOTES_SPLIT_MAX;
  if (width > 0) {
    lo = Math.max(lo, NOTES_PANE_MIN_PX / width);
    hi = Math.min(hi, 1 - NOTES_PANE_MIN_PX / width);
  }
  if (!(lo <= hi)) return NOTES_SPLIT_DEFAULT;   // too narrow for both floors: split evenly
  const r = Number(ratio);
  return Number.isFinite(r) ? Math.min(hi, Math.max(lo, r)) : NOTES_SPLIT_DEFAULT;
}

/* One custom property on the row, read by .notes-editor's flex-basis —
   nothing touches inline styles on the elements the CSS owns. Returns the
   ratio actually applied. */
export function applyNotesSplit(ratio) {
  const split = $('notesSplit');
  const r = clampNotesSplit(ratio, split.getBoundingClientRect().width);
  split.style.setProperty('--notes-split', (r * 100).toFixed(1) + '%');
  return r;
}

export function notesMode() {
  const split = $('notesSplit');
  return split ? split.dataset.mode : 'split';
}

/* Edit / Split / Preview: data-mode on the row does the showing and hiding
   (stylesheet), the three buttons mirror it. Entering a mode that shows
   the preview paints whatever was typed while it was hidden. */
export function setNotesMode(mode) {
  if (!NOTES_MODES.includes(mode)) mode = 'split';
  $('notesSplit').dataset.mode = mode;
  for (const [id, m] of [['btnNotesEdit', 'edit'], ['btnNotesSplit', 'split'], ['btnNotesPreview', 'preview']]) {
    const b = $(id);
    b.setAttribute('aria-pressed', String(m === mode));
    b.className = m === mode ? 'btn' : 'btn ghost';
  }
  if (mode !== 'edit' && previewStale) renderPreview();
}

/* Paint the editor into the preview. Wholesale innerHTML, which is what
   lets the link clicks be delegated (wireNotes) rather than bound per
   render. Skipped — and left marked stale — while nothing would show it
   (editor-only mode, or the page isn't up): setNotesMode/showNotesTab
   catch up the moment it is. The pane's own scrollTop is put back after
   the swap so editing the bottom of a long note doesn't jump the preview
   to its top on every keystroke. */
export function renderPreview() {
  const split = $('notesSplit');
  const view = $('notesview');
  if (!split || !view || view.hidden || split.dataset.mode === 'edit') return false;
  const pane = $('notesPreview');
  const top = pane.scrollTop;
  $('notesPreviewBody').innerHTML = renderMarkdown($('notesEditor').value);
  pane.scrollTop = top;
  previewStale = false;
  return true;
}

/* Live: every input event asks for a render, coalesced so a typing burst
   costs one. Top-level debounce is fine because it comes from core.js,
   which imports nothing (docs/notes/frontend-modules.md, rule 2). */
export const renderLive = debounce(renderPreview, 150);

/* The divider: pointer capture on the handle, like wirePagePanelResize
   (plugins.js) and the first_last plugin's rail — pointerdown must
   preventDefault or the drag selects text in the editor; pointercancel is
   handled so a cancelled touch/pen drag doesn't leave the handle stuck in
   its dragging state. The ratio persists on release; double-click resets. */
export function wireNotesDivider() {
  const handle = $('notesDivider');
  const split = $('notesSplit');
  if (!handle || !split) return;
  handle.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add('dragging');
    let ratio = null;
    const move = (ev) => {
      const rect = split.getBoundingClientRect();
      if (!rect.width) return;
      ratio = applyNotesSplit((ev.clientX - rect.left) / rect.width);
    };
    const up = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.removeEventListener('pointercancel', up);
      handle.classList.remove('dragging');
      if (ratio != null) saveNotesPrefs({ split: Math.round(ratio * 1000) / 1000 });
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
    handle.addEventListener('pointercancel', up);
  });
  handle.addEventListener('dblclick', () => saveNotesPrefs({ split: applyNotesSplit(NOTES_SPLIT_DEFAULT) }));
}

/* ------------------------------------------------------------- links */

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

export function insertAtCursor(text) {
  const ed = $('notesEditor');
  const at = ed.selectionStart ?? ed.value.length;
  ed.value = ed.value.slice(0, at) + text + ed.value.slice(ed.selectionEnd ?? at);
  ed.selectionStart = ed.selectionEnd = at + text.length;
  ed.focus();
  ed.dispatchEvent(new Event('input'));   // autosave sees it like typing
}

export function wireNotes() {
  $('tabNotes').onclick = showNotesTab;
  // One listener for every write path (typing, Link ▾, a plugin's
  // notesPage.setText/insert — all dispatch 'input'): autosave AND the
  // live preview follow it, so nothing can update one without the other.
  $('notesEditor').oninput = () => {
    $('notesSaved').textContent = 'Saving…';
    save();
    previewStale = true;
    renderLive();
  };
  $('btnNotesEdit').onclick = () => setNotesMode('edit');
  $('btnNotesSplit').onclick = () => setNotesMode('split');
  $('btnNotesPreview').onclick = () => setNotesMode('preview');
  $('btnNotesLink').onclick = () => {
    // The link lands at the editor's cursor — invisible in preview-only mode.
    if (notesMode() === 'preview') setNotesMode('split');
    insertNotesLink($('btnNotesLink'));
  };
  wireNotesDivider();
  applyNotesSplit(loadNotesPrefs().split);
  // Delegated — the preview re-renders wholesale (innerHTML) on every
  // edit, so a handler bound to a link would be gone after the next keystroke.
  $('notesPreview').addEventListener('click', (e) => {
    const a = e.target.closest('a.notes-link');
    if (!a) return;
    e.preventDefault();
    followWinnowLink(a.dataset.winnow);
  });
}

/* A case switch invalidates the loaded body — and the TEXT, which is the
   part that bit: clearing only the flag left the previous case's narrative
   in the editor, where showNotesTab's anti-clobber guard then refused to
   seed the new case's body over it. The analyst read case A's notes under
   case B's title, and the first keystroke autosaved them onto case B. */
export function resetNotes() {
  loaded = false;
  previewStale = true;
  const ed = $('notesEditor');
  if (ed) ed.value = '';
  // A renderLive still pending from case A reads the now-empty editor, so
  // it cannot repaint A's narrative here either.
  const body = $('notesPreviewBody');
  if (body) body.innerHTML = '';
}

/* The body loads lazily with the page. Anything that writes the editor
   before the analyst has opened Notes — a plugin's notesPage.insert —
   must go through here first, or its input event autosaves plugin text
   over a case body that was never fetched. */
export async function ensureNotesLoaded() {
  if (loaded) return;
  try {
    const r = await api('/api/case/notes');
    // Don't clobber text the analyst has already typed: the load is async,
    // so typing into a just-opened Notes tab can race ahead of it. Only
    // seed the editor from the saved body when it's still empty.
    if (!$('notesEditor').value) $('notesEditor').value = r.body || '';
    loaded = true;
    // Seeding assigns .value directly (no input event, or it would autosave
    // the body straight back), so the preview is told by hand.
    previewStale = true;
    renderPreview();
  } catch { /* leave whatever's there */ }
}

export async function showNotesTab() {
  recordTabVisit({ kind: 'page', key: 'notes' });
  S.activeTab = 'notes';
  showMainView('notesview');
  syncTabSelection();
  syncTabChrome();
  // Layout first, synchronously: showMainView has already revealed the
  // page, and neither the mode nor the divider position depends on the
  // body — applied after the fetch they'd paint the markup default and
  // then jump. The mode is always Split (not remembered, see NOTES_KEY);
  // the ratio is whatever the last drag left.
  setNotesMode('split');
  applyNotesSplit(loadNotesPrefs().split);
  await ensureNotesLoaded();
  if (previewStale) renderPreview();
  // Focusing a display:none textarea is a silent no-op, but say so.
  if (notesMode() !== 'preview') setTimeout(() => $('notesEditor').focus(), 0);
}
