/* The detail pane, its notes field, and the JSON/XML pretty-printing in it.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, debounce, el, post, toast } from './core.js';
import { addExtractedColumn, openFlattenModal } from './derived.js';
import { ellipsize, setColumnFilter, setSearchMode } from './filters.js';
import { render, rowAt } from './grid.js';
import { writeClipboardText } from './grouping.js';
import { syncSearchExpansion } from './search.js';
import { clearAllFilters } from './sources.js';
import { S } from './state.js';
import { displayCell } from './tsformat.js';
import { contextMenu } from './ui.js';
import { rebuildView } from './view.js';

/* --------------------------------------------------------------- detail */

/* Nested JSON/XML field values get pretty-printed + syntax colored in the
   detail pane (grid cells stay plain — single-line, truncated, virtualized).
   Everything here builds DOM nodes directly (never innerHTML) since field
   values are untrusted forensic data that can contain HTML-looking text. */

export function tryParseJSON(v) {
  const s = v.trim();
  if (!s || (s[0] !== '{' && s[0] !== '[')) return null;
  try { return JSON.parse(s); } catch { return null; }
}

export function looksLikeXml(v) {
  const s = v.trim();
  return s.startsWith('<') && s.endsWith('>') && /<\/?[a-zA-Z_][\w:.-]*[^>]*>/.test(s);
}

export function prettyXml(xml) {
  // Heuristic reflow, not a real parser: forensic XML fragments are often
  // malformed/truncated, so this must degrade gracefully rather than throw.
  const tags = xml.replace(/>\s*</g, '><').split(/(?=<)/).filter(Boolean);
  let out = '', depth = 0;
  for (const tag of tags) {
    const isClosing = tag.startsWith('</');
    const isVoid = /\/>$/.test(tag) || tag.startsWith('<?') || tag.startsWith('<!');
    if (isClosing) depth = Math.max(0, depth - 1);
    out += '  '.repeat(depth) + tag + '\n';
    if (!isClosing && !isVoid) depth++;
  }
  return out.trim();
}

/* The unhighlighted-but-readable fallback for XML the browser's own
   parser rejects — truncated fragments, mismatched tags, the shapes real
   evidence actually contains. Regex-based on purpose: it has to degrade
   rather than throw, which is exactly what a parser can't do. Nothing it
   emits carries a path, because there is no trustworthy structure to
   address in a document that didn't parse. */
export function appendXmlHighlighted(container, xmlText) {
  const tagRe = /<(\/?)([a-zA-Z_][\w:.-]*)((?:\s+[a-zA-Z_][\w:.-]*\s*=\s*"[^"]*")*)\s*(\/?)>/g;
  const attrRe = /([a-zA-Z_][\w:.-]*)(\s*=\s*)("[^"]*")/g;
  let last = 0, m;
  while ((m = tagRe.exec(xmlText))) {
    if (m.index > last) container.append(xmlText.slice(last, m.index));
    container.append('<' + m[1]);
    container.append(el('span', 'xtok-tag', m[2]));
    const attrs = m[3];
    if (attrs) {
      let aLast = 0, am;
      attrRe.lastIndex = 0;
      while ((am = attrRe.exec(attrs))) {
        if (am.index > aLast) container.append(attrs.slice(aLast, am.index));
        container.append(el('span', 'xtok-attr', am[1]));
        container.append(am[2]);
        container.append(el('span', 'xtok-attrval', am[3]));
        aLast = am.index + am[0].length;
      }
      container.append(attrs.slice(aLast));
    }
    container.append(m[4] + '>');
    last = m.index + m[0].length;
  }
  container.append(xmlText.slice(last));
}

/* ------------------ path-aware pretty-printing

   The pretty-printer builds from the *parsed* document rather than
   regexing over its serialized text, because every node it emits carries
   the path that addresses it (`data-path`) on the span. That path is what
   makes "right-click a field → add it as a column" a single click instead
   of a syntax the analyst has to learn and type: the same string the
   backend's json_field/xml_field operations take is already sitting on the
   node under the pointer.

   Everything appends DOM nodes and never innerHTML — field values are
   untrusted forensic data that routinely contains HTML-looking text. */

export function jsonPathStep(base, seg) {
  if (typeof seg === 'number') return `${base}[${seg}]`;
  return /^[^.[\]"']+$/.test(seg) ? `${base}.${seg}` : `${base}["${String(seg).replace(/(["\\])/g, '\\$1')}"]`;
}

export function jsonTokenClass(v) {
  if (v === null) return 'jtok-null';
  if (typeof v === 'boolean') return 'jtok-bool';
  if (typeof v === 'number') return 'jtok-num';
  return 'jtok-str';
}

/* A span the detail menu can act on: the path that addresses it, and the
   raw (unformatted) value it holds, so "filter to this" filters to what's
   in the data rather than to its pretty-printed rendering. */
export function fieldNode(cls, text, path, value, kind) {
  const n = el('span', cls, text);
  if (path) {
    n.dataset.path = path;
    n.dataset.structKind = kind;
    if (value !== undefined) n.dataset.value = value;
    n.classList.add('struct-node');
  }
  return n;
}

export function appendJsonNodes(container, value, path, indent) {
  const pad = '  '.repeat(indent);
  const padIn = '  '.repeat(indent + 1);
  if (Array.isArray(value)) {
    if (!value.length) { container.append('[]'); return; }
    container.append('[\n');
    value.forEach((v, i) => {
      container.append(padIn);
      appendJsonNodes(container, v, jsonPathStep(path, i), indent + 1);
      container.append(i < value.length - 1 ? ',\n' : '\n');
    });
    container.append(pad + ']');
    return;
  }
  if (value && typeof value === 'object') {
    const keys = Object.keys(value);
    if (!keys.length) { container.append('{}'); return; }
    container.append('{\n');
    keys.forEach((k, i) => {
      const kp = jsonPathStep(path, k);
      const leaf = value[k];
      const scalar = !leaf || typeof leaf !== 'object';
      container.append(padIn);
      // The key carries the path too, so clicking either half of
      // `"user": "jacson"` means the same field.
      container.append(fieldNode('jtok-key', JSON.stringify(k), kp,
        scalar ? jsonLeafText(leaf) : undefined, 'json'));
      container.append(': ');
      appendJsonNodes(container, leaf, kp, indent + 1);
      container.append(i < keys.length - 1 ? ',\n' : '\n');
    });
    container.append(pad + '}');
    return;
  }
  container.append(fieldNode(jsonTokenClass(value), JSON.stringify(value), path,
    jsonLeafText(value), 'json'));
}

/* Mirrors structparse._leaf_text — what the extracted column would hold,
   which is what the filter/copy actions should act on. */
export function jsonLeafText(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/* XML is rendered from a real parse when the browser can manage one, and
   falls back to the old heuristic reflow when it can't — forensic XML
   fragments are often malformed or truncated, and degrading to
   "unhighlighted but readable" beats showing nothing. Only the parsed path
   carries `data-path`; there is nothing trustworthy to address in a
   document that didn't parse. */
export function parseXmlDoc(text) {
  const body = text.trim();
  // Same guard as structparse.load_xml: a DOCTYPE is refused rather than
  // parsed. DOMParser won't expand external entities, but keeping the two
  // sides on the same rule means the detail pane never offers a path the
  // backend would then decline to extract.
  if (/<!DOCTYPE/i.test(body)) return null;
  const parse = (t) => {
    const doc = new DOMParser().parseFromString(t, 'application/xml');
    return doc.querySelector('parsererror') ? null : doc.documentElement;
  };
  return parse(body)
    || parse(`<winnow-fragment>${body.replace(/^<\?xml[^>]*\?>/, '')}</winnow-fragment>`);
}

export function xmlLocal(name) { return name.includes(':') ? name.split(':').pop() : name; }

export const XML_ID_ATTRS = ['name', 'key', 'id'];

/* The frontend twin of structparse._sibling_selectors — repeated elements
   that carry an identifying attribute are addressed by it rather than by
   position, so an EVTX `<Data Name="LogonType">` offers a path that means
   the same thing in every row. Kept in step with the backend: a path this
   produces has to be one xml_field can resolve. */
export function xmlSiblingSelectors(kids) {
  const groups = new Map();
  kids.forEach((c, i) => {
    const tag = xmlLocal(c.tagName);
    if (!groups.has(tag)) groups.set(tag, []);
    groups.get(tag).push(i);
  });
  const sel = new Array(kids.length).fill(0);
  for (const idxs of groups.values()) {
    let chosen = null;
    if (idxs.length > 1) {
      for (const want of XML_ID_ATTRS) {
        const real = idxs.map((i) => [...kids[i].attributes].find((a) => xmlLocal(a.name).toLowerCase() === want));
        const vals = real.map((a) => (a ? a.value : null));
        if (vals.every((v) => v !== null) && new Set(vals).size === vals.length) {
          chosen = { attr: xmlLocal(real[0].name), vals };
          break;
        }
      }
    }
    idxs.forEach((i, slot) => { sel[i] = chosen ? [chosen.attr, chosen.vals[slot]] : slot; });
  }
  return sel;
}

export function xmlStep(tag, sel) {
  if (Array.isArray(sel)) return `${tag}[@${sel[0]}='${sel[1]}']`;
  return sel === 0 ? tag : `${tag}[${sel}]`;
}

export function appendXmlNodes(container, node, path, indent) {
  const pad = '  '.repeat(indent);
  const tag = xmlLocal(node.tagName);
  const selector = path.endsWith(']') && /\[@[\w:.-]+='[^']*'\]$/.test(path);
  const kids = [...node.children];
  // A leaf element's text is its value, and the tag is what most people
  // aim at — so the opening tag carries it too, rather than only the text
  // run and the closing tag. Computed before anything is emitted because
  // the opening tag is written first.
  const leafText = kids.length ? undefined : (node.textContent || '').trim();
  container.append(pad + '<');
  container.append(fieldNode('xtok-tag', tag, path, leafText, 'xml'));
  for (const a of node.attributes) {
    const an = xmlLocal(a.name);
    container.append(' ');
    // An attribute that selected this element restates its own predicate;
    // it still renders, it just isn't offered as a separate field.
    const apath = selector && path.includes(`@${an}='${a.value}'`) ? null : `${path}@${an}`;
    container.append(fieldNode('xtok-attr', an, apath, a.value, 'xml'));
    container.append('=');
    container.append(fieldNode('xtok-attrval', `"${a.value}"`, apath, a.value, 'xml'));
  }
  if (!kids.length) {
    if (!leafText) { container.append('/>\n'); return; }
    container.append('>');
    container.append(fieldNode('xtok-text', leafText, path, leafText, 'xml'));
    container.append('</');
    container.append(fieldNode('xtok-tag', tag, path, leafText, 'xml'));
    container.append('>\n');
    return;
  }
  container.append('>\n');
  const sel = xmlSiblingSelectors(kids);
  kids.forEach((child, i) => {
    appendXmlNodes(container, child, `${path ? path + '/' : ''}${xmlStep(xmlLocal(child.tagName), sel[i])}`, indent + 1);
  });
  container.append(pad + '</');
  container.append(fieldNode('xtok-tag', tag, path, undefined, 'xml'));
  container.append('>\n');
}

export function renderDetailContent(v) {
  const json = tryParseJSON(v);
  if (json !== null && typeof json === 'object') {
    const pre = el('pre', 'detail-pretty');
    appendJsonNodes(pre, json, '$', 0);
    return pre;
  }
  if (looksLikeXml(v)) {
    const root = parseXmlDoc(v);
    if (root) {
      const pre = el('pre', 'detail-pretty');
      // A synthetic fragment wrapper is ours, not the document's — its
      // children are what the analyst actually has, and what paths are
      // written against.
      if (root.tagName === 'winnow-fragment') {
        const kids = [...root.children];
        const sel = xmlSiblingSelectors(kids);
        kids.forEach((c, i) => appendXmlNodes(pre, c, xmlStep(xmlLocal(c.tagName), sel[i]), 0));
      } else {
        appendXmlNodes(pre, root, xmlLocal(root.tagName), 0);
      }
      return pre;
    }
    try {
      // Unparseable: the old heuristic reflow, highlighted but not
      // addressable.
      const pre = el('pre', 'detail-pretty');
      appendXmlHighlighted(pre, prettyXml(v));
      return pre;
    } catch { /* malformed fragment — fall through to plain text */ }
  }
  return document.createTextNode(v);
}

/* The detail pane only force-opens on double-click (activateRow's plain
   single-click path deliberately never calls showDetail) or the toggleDetail
   hotkey. Once it's open, though, cursor movement — click, arrow keys,
   ctrl/cmd-click — should keep it in sync with whatever row is now current,
   which is what this gates. */
export function maybeShowDetail(pos) {
  if (!$('detail').hidden) showDetail(pos);
}

export function showDetail(pos) {
  const r = rowAt(pos);
  const d = $('detail');
  if (!r) { d.hidden = true; $('detailResize').hidden = true; return; }
  d.hidden = false;
  $('detailResize').hidden = false;
  $('detailTitle').textContent = `Line ${r.rid}`;
  const dl = $('detailFields');
  dl.replaceChildren();
  S.columns.forEach((c, i) => {
    const v = r.cells[i];
    if (v == null || v === '') return;
    const dt = el('dt', null, c.name);
    dt.dataset.col = c.name;
    dl.append(dt);
    const dd = el('dd');
    // Which column a selection or a clicked node belongs to — the detail
    // menu's actions (filter, exclude, add as column) all need it, and the
    // <dd> is the only thing that still knows once you're deep inside a
    // pretty-printed document.
    dd.dataset.col = c.name;
    dd.append(renderDetailContent(displayCell(c.name, v)));
    dl.append(dd);
  });
  const note = $('noteInput');
  note.value = r.note || '';
  note.dataset.rid = r.rid;
  note.dataset.sourceId = r.source_id;
  $('noteStatus').textContent = '';
}

/* ------------------------------------------------- detail pane menu

   Right-clicking in the detail pane answers whichever of two questions the
   pointer is actually over, and often both at once:

   - text is selected → act on that substring (copy it, filter the column
     to it, search the table for it). A selection inside a JSON blob is a
     fragment, so it filters as *contains*, never as `=`: the cell it came
     from is a whole document and an exact match would return nothing.
   - the click landed on a node of a parsed JSON/XML document → act on that
     field (add it as a column, filter to its exact value, copy it). This
     is the path that makes extraction a click rather than a syntax to
     learn — `data-path` was put on the node by the pretty-printer.

   Both are scoped to the column the <dd> belongs to. */

export function detailSelectionText() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) return '';
  const node = sel.anchorNode;
  const holder = node && (node.nodeType === 1 ? node : node.parentElement);
  // A selection that started outside the detail fields isn't ours.
  if (!holder || !holder.closest('#detailFields')) return '';
  return String(sel).trim();
}

export function detailMenuItems(ctx) {
  const items = [];
  const { column, selection, node } = ctx;

  if (selection) {
    const shown = ellipsize(selection);
    items.push({ header: `Selection in ${column}` });
    items.push({
      label: 'Copy',
      onclick: () => writeClipboardText(Promise.resolve(selection), 'Copied selection'),
    });
    items.push({
      label: `Filter ${column} to ${shown}`,
      title: 'Filters to rows whose value contains this text',
      onclick: () => filterByContains(column, selection),
    });
    items.push({
      label: `Filter ${column} to ${shown} only`,
      title: 'Drops every other filter and the search — the timeframe filter stays',
      onclick: () => filterByContains(column, selection, { only: true }),
    });
    items.push({
      label: `Exclude ${shown}`,
      onclick: () => filterByContains(column, selection, { exclude: true }),
    });
    items.push({
      label: `Search all columns for ${shown}`,
      onclick: () => searchForText(selection),
    });
  }

  if (node && node.dataset.path) {
    const path = node.dataset.path;
    const value = node.dataset.value;
    const kind = node.dataset.structKind;
    if (items.length) items.push('-');
    items.push({ header: ellipsize(path, 48), literal: true });
    items.push({
      label: 'Add as a column',
      title: `Adds a column holding ${path} from every row of "${column}"`,
      onclick: () => addExtractedColumn(column, path, kind),
    });
    if (value !== undefined && value !== '') {
      items.push({
        label: `Filter ${column} to this value`,
        title: 'Filters to rows whose document contains this value',
        onclick: () => filterByContains(column, value),
      });
      items.push({
        label: 'Copy value',
        onclick: () => writeClipboardText(Promise.resolve(value), 'Copied value'),
      });
    }
    items.push({ label: 'Copy path', onclick: () => writeClipboardText(Promise.resolve(path), 'Copied path') });
    items.push({
      label: 'Flatten this document into columns…',
      onclick: () => openFlattenModal(column),
    });
  }
  return items;
}

/* Contains-filtering, as distinct from filterByValue's `=`. A selection or
   a field pulled out of a document is a *part* of the cell, so the filter
   that finds it again is the substring one. Reuses the same raw-filter
   syntax the header boxes take, and the same clearAllFilters seeding that
   makes `only` correct (the timeframe filter survives, grouping is
   stashed) rather than reimplementing either. */
export async function filterByContains(column, text, { only = false, exclude = false } = {}) {
  const value = String(text == null ? '' : text);
  if (!value) return;
  if (/^\s|\s$/.test(value)) {
    toast('That selection starts or ends with whitespace — trim it and try again', 5000);
    return;
  }
  const raw = (exclude ? '!' : '') + value;
  const shown = ellipsize(value);
  if (only) {
    await clearAllFilters({ column, raw });
    toast(`Filtered ${column} to ${shown} · other filters cleared`);
    return;
  }
  setColumnFilter(column, raw);
  await rebuildView();
  toast(`Filtered ${column} ${exclude ? 'not ' : ''}containing ${shown}`);
}

export async function searchForText(text) {
  const value = String(text || '').trim();
  if (!value) return;
  // 'contains' is the plain-substring mode (see #searchModeToggle) — a
  // selection lifted out of a document is literal text, so regex mode
  // would treat its dots and brackets as syntax.
  if (S.searchMode !== 'contains') await setSearchMode('contains');
  S.search = value;
  $('search').value = value;
  syncSearchExpansion(true);
  await rebuildView({ keepScroll: false });
  toast(`Searching for ${ellipsize(value)}`);
}

export const saveNote = debounce(async () => {
  const note = $('noteInput');
  const rid = Number(note.dataset.rid);
  const sourceId = Number(note.dataset.sourceId);
  if (!rid) return;
  await post('/api/note', { source_id: sourceId, rid, note: note.value });
  const r = rowAt(S.cursor);
  if (r && r.rid === rid) r.note = note.value;
  $('noteStatus').textContent = 'Saved';
  render();
}, 500);

/* ------------------------------------------------------------- detail pane */

/* Dock side + size are a browser-local UI preference, not case data — same
   rationale as the keymap/appearance blocks below (not saved in
   workspace/, doesn't travel with the case). */
export const DETAIL_KEY = 'winnow.detail';

export function loadDetailPrefs() {
  try { return { dock: 'bottom', size: null, ...JSON.parse(localStorage.getItem(DETAIL_KEY) || '{}') }; }
  catch { return { dock: 'bottom', size: null }; }
}

export function saveDetailPrefs() { localStorage.setItem(DETAIL_KEY, JSON.stringify(S.detailPrefs)); }

export function applyDetailPrefs() {
  const area = $('mainArea');
  const d = $('detail');
  area.dataset.dock = S.detailPrefs.dock;
  if (S.detailPrefs.size) {
    if (S.detailPrefs.dock === 'right') { d.style.width = S.detailPrefs.size + 'px'; d.style.height = ''; }
    else { d.style.height = S.detailPrefs.size + 'px'; d.style.width = ''; }
  } else {
    d.style.width = ''; d.style.height = '';
  }
  $('btnDetailDock').title = S.detailPrefs.dock === 'right' ? 'Dock to the bottom' : 'Dock to the right';
}

export function toggleDetailPane() {
  const d = $('detail');
  if (d.hidden) { if (S.cursor >= 0 && rowAt(S.cursor)) showDetail(S.cursor); }
  else { d.hidden = true; $('detailResize').hidden = true; }
}

/* DOM wiring for this module, called once by main.js. Handlers can't
   fire during load, so the order these run in doesn't matter — the
   startup steps that DO depend on order live in main.js instead. */
export function wireDetail() {
$('detail').addEventListener('contextmenu', (e) => {
  const dd = e.target.closest('#detailFields dd, #detailFields dt');
  if (!dd || !dd.dataset.col) return; // the note box and the header keep the browser's menu
  const ctx = {
    column: dd.dataset.col,
    selection: detailSelectionText(),
    node: e.target.closest('.struct-node'),
  };
  const items = detailMenuItems(ctx);
  if (!items.length) return;
  e.preventDefault();
  contextMenu(e, items);
});

$('btnDetailDock').onclick = () => {
  S.detailPrefs.dock = S.detailPrefs.dock === 'right' ? 'bottom' : 'right';
  S.detailPrefs.size = null; // switching axis — the old size doesn't mean anything on the new one
  applyDetailPrefs();
  saveDetailPrefs();
};

$('detailResize').addEventListener('mousedown', (e) => {
  e.preventDefault();
  const dock = S.detailPrefs.dock;
  const d = $('detail');
  const handle = $('detailResize');
  const startPos = dock === 'right' ? e.clientX : e.clientY;
  const startSize = dock === 'right' ? d.getBoundingClientRect().width : d.getBoundingClientRect().height;
  handle.classList.add('dragging');
  const move = (ev) => {
    const pos = dock === 'right' ? ev.clientX : ev.clientY;
    // Dragged inward from the edge the pane is docked against — right dock's
    // handle sits on its left edge, bottom dock's on its top edge, so in
    // both cases moving the handle *toward* that edge should *grow* the pane.
    const delta = startPos - pos;
    const size = Math.max(200, startSize + delta);
    if (dock === 'right') d.style.width = size + 'px';
    else d.style.height = size + 'px';
    S.detailPrefs.size = size;
  };
  const up = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    handle.classList.remove('dragging');
    saveDetailPrefs();
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
});

$('btnCloseDetail').onclick = () => { $('detail').hidden = true; $('detailResize').hidden = true; };

$('btnCopyRow').onclick = () => {
  const r = rowAt(S.cursor);
  if (!r) return;
  const text = S.columns.map((c, i) => `${c.name}: ${r.cells[i] ?? ''}`).join('\n');
  writeClipboardText(Promise.resolve(text), 'Row copied');
};

$('noteInput').oninput = () => { $('noteStatus').textContent = 'Saving…'; saveNote(); };
}
