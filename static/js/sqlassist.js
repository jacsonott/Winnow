/* SQL pane assists: autocomplete, the query-box resize bar, the Tables
   insert menu, and the tags lookup that joins row_tags onto a result.

   All client-side — the /api/sql contract (read-only, no params) is
   untouched; even the tags column is fetched through a second ordinary
   query against row_tags. Dependency-free by CLAUDE.md's rule, so the
   autocomplete dropdown and the caret measurement are hand-rolled. */
import { $, el, post } from './core.js';
import { sourceLabel } from './sources.js';
import { S } from './state.js';
import { dropdownMenu } from './ui.js';

const SQL_KEYWORDS = [
  'SELECT', 'FROM', 'WHERE', 'GROUP BY', 'ORDER BY', 'LIMIT', 'OFFSET',
  'LEFT JOIN', 'JOIN', 'ON', 'AS', 'AND', 'OR', 'NOT', 'IN', 'LIKE', 'GLOB',
  'BETWEEN', 'IS NULL', 'IS NOT NULL', 'DISTINCT', 'HAVING', 'UNION ALL',
  'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'ASC', 'DESC',
  'COUNT(', 'SUM(', 'AVG(', 'MIN(', 'MAX(', 'GROUP_CONCAT(', 'CAST(',
  'COALESCE(', 'LOWER(', 'UPPER(', 'SUBSTR(', 'LENGTH(',
  'TS_NORMALIZE(', 'DAY_BUCKET(',
  'row_tags', 'row_notes', 'tag_defs',
];

const quoteIdent = (name) => (/^[A-Za-z_]\w*$/.test(name) ? name : '"' + name.replace(/"/g, '""') + '"');

/* ------------------------------------------------------------ suggestions */

function referencedSourceIds(text) {
  return [...new Set([...text.matchAll(/\bsrc_(\d+)\b/g)].map((m) => Number(m[1])))];
}

export function sqlSuggestions(text, word) {
  const w = word.toLowerCase();
  const out = [];
  const seen = new Set();
  const push = (label, insert, kind) => {
    if (seen.has(insert)) return;
    seen.add(insert);
    out.push({ label, insert, kind });
  };
  for (const s of S.sources) {
    if (s.is_merge || s.error || s.id < 0) continue;
    const table = `src_${s.id}`;
    const name = sourceLabel(s);
    if (name.toLowerCase().startsWith(w) || table.startsWith(w)) {
      push(`${name} — ${table}`, table, 'table');
    }
  }
  // Columns from the tables the query mentions; before any table is typed,
  // fall back to the table open in the grid.
  const ids = referencedSourceIds(text);
  const colSources = ids.length
    ? S.sources.filter((s) => ids.includes(s.id))
    : S.sources.filter((s) => s.id === S.sourceId);
  for (const s of colSources) {
    for (const c of s.columns || []) {
      if (c.name.toLowerCase().startsWith(w)) push(c.name, quoteIdent(c.name), 'column');
    }
  }
  for (const k of SQL_KEYWORDS) {
    if (k.toLowerCase().startsWith(w)) push(k, k, 'keyword');
  }
  return out.slice(0, 12);
}

/* Caret pixel position inside a textarea, mirror-div technique: same font,
   width and wrapping, text up to the caret, a marker span at the end. */
const MIRROR_PROPS = ['fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'letterSpacing',
  'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
  'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth', 'boxSizing'];

function caretXY(ta) {
  const div = el('div');
  const cs = getComputedStyle(ta);
  for (const p of MIRROR_PROPS) div.style[p] = cs[p];
  div.style.cssText += `;position:fixed;left:-9999px;top:0;width:${ta.clientWidth}px;`
    + 'white-space:pre-wrap;overflow-wrap:break-word;visibility:hidden';
  div.textContent = ta.value.slice(0, ta.selectionStart);
  const mark = el('span', null, '​');
  div.append(mark);
  document.body.append(div);
  const xy = { top: mark.offsetTop - ta.scrollTop, left: mark.offsetLeft - ta.scrollLeft };
  div.remove();
  return xy;
}

/* --------------------------------------------------------------- dropdown */

let acEl = null;
let acItems = [];
let acIdx = 0;
let acWordStart = 0;

export function sqlAcOpen() { return !!acEl; }

function acHide() {
  if (acEl) acEl.remove();
  acEl = null;
  acItems = [];
}

function acPaint(ta) {
  if (!acEl) {
    acEl = el('div', 'menu sql-ac');
    document.body.append(acEl);
  }
  acEl.replaceChildren();
  acItems.forEach((it, i) => {
    const row = el('button', 'menu-item' + (i === acIdx ? ' sql-ac-active' : ''));
    row.append(el('span', null, it.label), el('span', 'count', it.kind));
    // mousedown, not click: click would blur the textarea first and the
    // blur handler would tear the dropdown down before the click lands.
    row.onmousedown = (e) => { e.preventDefault(); acAccept(ta, it); };
    acEl.append(row);
  });
  const r = ta.getBoundingClientRect();
  const xy = caretXY(ta);
  const line = parseInt(getComputedStyle(ta).lineHeight, 10) || 18;
  acEl.style.top = Math.min(r.top + xy.top + line + 4, window.innerHeight - acEl.offsetHeight - 8) + 'px';
  acEl.style.left = Math.min(r.left + xy.left, window.innerWidth - acEl.offsetWidth - 8) + 'px';
}

function acAccept(ta, item) {
  ta.setRangeText(item.insert, acWordStart, ta.selectionStart, 'end');
  acHide();
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ta.focus();
}

function acRefresh(ta, { force = false } = {}) {
  const upto = ta.value.slice(0, ta.selectionStart);
  const m = /[\w"]+$/.exec(upto);
  const word = m ? m[0] : '';
  if (!force && word.length < 2) { acHide(); return; }
  acWordStart = ta.selectionStart - word.length;
  acItems = sqlSuggestions(ta.value, word.replace(/"/g, ''));
  acIdx = 0;
  if (!acItems.length) { acHide(); return; }
  acPaint(ta);
}

export function wireSqlAssist() {
  const ta = $('sqlText');

  // ---- autocomplete
  ta.addEventListener('input', () => acRefresh(ta));
  ta.addEventListener('blur', () => setTimeout(acHide, 150));
  ta.addEventListener('click', acHide);
  ta.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === ' ') {
      e.preventDefault();
      acRefresh(ta, { force: true });
      return;
    }
    if (!acEl) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); acIdx = (acIdx + 1) % acItems.length; acPaint(ta); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); acIdx = (acIdx + acItems.length - 1) % acItems.length; acPaint(ta); }
    else if (e.key === 'Tab' || (e.key === 'Enter' && !e.ctrlKey && !e.metaKey)) {
      e.preventDefault();
      e.stopImmediatePropagation();
      acAccept(ta, acItems[acIdx]);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopImmediatePropagation(); // consumed here — must not clear the grid selection behind the pane
      acHide();
    }
  });

  // ---- the query box grows with content; the bar under it drags anywhere
  let manualH = 0;
  const autoGrow = () => {
    const max = Math.round(window.innerHeight * 0.45);
    ta.style.height = 'auto';
    const fit = Math.min(ta.scrollHeight + 2, max);
    ta.style.height = Math.max(fit, manualH) + 'px';
  };
  ta.addEventListener('input', autoGrow);
  const bar = $('sqlResize');
  bar.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    bar.setPointerCapture(e.pointerId);
    const startY = e.clientY;
    const startH = ta.offsetHeight;
    const move = (ev) => {
      manualH = Math.max(60, Math.min(startH + (ev.clientY - startY), Math.round(window.innerHeight * 0.7)));
      ta.style.height = manualH + 'px';
    };
    const up = () => {
      bar.removeEventListener('pointermove', move);
      bar.removeEventListener('pointerup', up);
    };
    bar.addEventListener('pointermove', move);
    bar.addEventListener('pointerup', up);
  });

  // ---- "which src_N is my table?" — the insert menu
  $('btnSqlTables').onclick = () => dropdownMenu($('btnSqlTables'), () => {
    const items = [{ header: 'Click to insert at the cursor' }];
    for (const s of S.sources) {
      if (s.is_merge || s.error || s.id < 0) continue;
      items.push({
        label: `${sourceLabel(s)} — src_${s.id} (${(s.row_count || 0).toLocaleString()} rows)`,
        onclick: () => {
          ta.setRangeText(`src_${s.id}`, ta.selectionStart, ta.selectionEnd, 'end');
          ta.dispatchEvent(new Event('input', { bubbles: true }));
          ta.focus();
        },
      });
    }
    if (items.length === 1) items.push({ header: 'No tables in this case yet', literal: true });
    return items;
  });
}

/* ------------------------------------------------------------------- tags */

/* A Tags column for a result, when the result can carry one: the query
   reads exactly one src_N and the result includes a `rid` column. Fetched
   as a second ordinary read-only query against row_tags — same trust and
   same cap as the query it decorates. */
export async function sqlTagsFor(r, sql) {
  const ids = referencedSourceIds(sql);
  const ridIdx = r.columns.findIndex((c) => String(c).toLowerCase() === 'rid');
  if (ids.length !== 1 || ridIdx === -1 || !r.rows.length) return null;
  const rids = [...new Set(r.rows.map((row) => Number(row[ridIdx])).filter(Number.isInteger))];
  if (!rids.length || rids.length > 5000) return null;
  try {
    const res = await post('/api/sql', {
      sql: `SELECT rid, GROUP_CONCAT(tag_id) FROM row_tags WHERE source_id=${ids[0]} `
        + `AND rid IN (${rids.join(',')}) GROUP BY rid`,
    });
    const map = {};
    for (const [rid, t] of res.rows) {
      map[rid] = String(t == null ? '' : t).split(',').filter(Boolean).map(Number);
    }
    return { ridIdx, map };
  } catch {
    return null; // decoration only — the result itself already painted fine
  }
}

export function tagChips(tagIds) {
  const wrap = el('span', 'sql-tag-cell');
  for (const id of tagIds || []) {
    const t = S.tags.find((x) => x.id === id);
    const chip = el('span', 'sql-tag-chip', t ? t.name : `tag ${id}`);
    if (t) chip.style.background = t.color;
    wrap.append(chip);
  }
  return wrap;
}
