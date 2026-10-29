/* Pivot table tab — Excel's PivotTable, over an ingested Winnow table.

   Drag fields from the list into Filters / Columns / Rows / Values; the
   cross-tab rebuilds itself. Everything visual comes from Winnow's CSS
   tokens, so all four styles and both themes work without a stylesheet of
   our own.

   The one piece worth understanding before changing anything: subtotals are
   *queried*, not summed from the cells above them. Each level of the row
   nesting is its own grouping set in the request (see the backend's
   docstring) because re-adding leaf values gives the wrong answer for a
   distinct count, and a total that's wrong only sometimes is worse than no
   total at all. `lookup()` is the single accessor over all of them. */

const BLANK = '(blank)';           // Excel's spelling for an empty grouping key
const REFRESH_MS = 250;            // debounce after a field change
const MAX_COLS = 60;               // rendered column groups before we stop and say so

/* Built once, not per call. localeCompare and toLocaleString construct an
   Intl.Collator or Intl.NumberFormat every time they are called, and a
   pivot of a few thousand rows calls them hundreds of thousands of times
   between its sorts and its cells: sorting 4,000 keys took 265 ms through
   localeCompare with options and 16 ms through one shared collator. The
   same locale and options, so the same order and the same text. */
const byKey = new Intl.Collator(undefined, { numeric: true }).compare;
const byText = new Intl.Collator().compare;
const wholeNumber = new Intl.NumberFormat();
const twoDecimals = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

/* The tag filter, spelled as the grid's ribbon spells it (S.tagFilter in
   static/js/tags.js) so a chip means the same thing on both: a tag id keeps
   rows carrying it, TAG_ANY rows carrying any tag, TAG_NONE rows carrying
   none — those OR'd, as two chips are on the ribbon — and '!<id>' drops
   rows carrying that tag, AND'd onto the rest. The backend's _tag_clauses
   compiles exactly this list. */
const TAG_ANY = '__any__';
const TAG_NONE = '__none__';
const tagExcludeKey = (id) => `!${id}`;
const isTagExclusion = (key) => typeof key === 'string' && key.startsWith('!');
const tagIdOf = (key) => (isTagExclusion(key) ? Number(key.slice(1)) : key);

/* ------------------------------------------------ saved pivots */

/* The pivots survive the case being closed, because they live in the case
   file: winnow.tabState (plugin API 10) is this mount's own row, written
   as the analyst works. There is no teardown callback in the contract, so
   saving on close is not something a plugin can do — it saves as it goes.

   DEFINITIONS ONLY, and validated on the way back in. The cross-tab itself
   is re-queried against the case as it stands now; a saved one would be a
   picture of the evidence rather than the evidence. Two things a payload
   cannot be trusted about: the table (a dropped source's id goes to the
   next import, so the id is only believed when the name still matches) and
   the fields (a column can be gone, and a grouping on a column the table
   no longer has is a 400 from the backend's own checks). */

const STATE_VERSION = 1;
const MAX_SAVED_PIVOTS = 24;

const emptySpec = (name) => ({
  name, sourceId: null, rows: [], cols: [], values: [], filters: [], tags: [],
  subtotals: true, grandTotals: true,
});

/* How a pivot names its table, in two shapes because tables come in two
   kinds. A real source's `name` is the imported file's own and nothing
   edits it (a nickname is a separate field), so it is what makes the id
   believable after SQLite has handed that id to the next import. A MERGE
   has no file: its name IS its display name and Rename this merge rewrites
   it (Store.set_source_nickname), so a name check there would read a
   rename as "that table is gone". Its member tables are the identity that
   survives a rename — and that tells it apart from a different merge that
   took its id after a delete. */
function savedSource(src) {
  if (!src) return null;
  const out = { id: src.id, name: src.name };
  if (src.is_merge) out.members = (src.member_source_ids || []).map(Number);
  return out;
}

/* One pivot, cut down to the spec: no data, no index, no meta, no
   loading/error/elapsed. Those describe a moment, not a question. */
export function pivotSpec(p, sources) {
  const src = (sources || []).find((s) => s.id === p.sourceId) || null;
  return {
    name: p.name,
    source: savedSource(src),
    rows: p.rows, cols: p.cols, values: p.values, filters: p.filters, tags: p.tags,
    subtotals: p.subtotals, grandTotals: p.grandTotals,
  };
}

const sameMembers = (a, b) => {
  if (!Array.isArray(a) || !Array.isArray(b) || !a.length || a.length !== b.length) return false;
  const x = a.map(Number).sort((p, q) => p - q);
  const y = b.map(Number).sort((p, q) => p - q);
  return x.every((n, i) => n === y[i]);
};

function resolveSource(saved, sources) {
  if (!saved || typeof saved !== 'object') return null;
  const name = saved.name == null ? null : String(saved.name);
  if (saved.members) {
    // A merge — matched on its members, never its name (savedSource says
    // why). Its own id first, so two merges over the same tables stay
    // apart; then any merge with those members, which is the merge rebuilt
    // under a new id.
    const same = sources.filter((s) => s.is_merge && sameMembers(saved.members, s.member_source_ids));
    return same.find((s) => s.id === saved.id) || same[0] || null;
  }
  const byId = sources.find((s) => s.id === saved.id) || null;
  if (byId && (name === null || byId.name === name)) return byId;
  // Re-imported under a new id: here the name is the stronger identity.
  return (name === null ? null : sources.find((s) => s.name === name)) || null;
}

/* A saved tag filter, against the case's tags as they are now. Tag ids are
   never reused (tag_defs is AUTOINCREMENT), so an id that is still listed is
   still the tag that was meant, and one that isn't was deleted: dropped, and
   said, because "only rows tagged X" against a tag nothing carries any more
   would be a pivot of nothing. A list that can't be read (`tags` null) is
   kept as saved, and the backend refuses a dead id by name. */
function restoreTags(keys, tags, gone) {
  const out = [];
  const live = Array.isArray(tags) ? new Map(tags.map((t) => [t.id, t])) : null;
  for (const key of Array.isArray(keys) ? keys : []) {
    let kept = null;
    if (key === TAG_ANY || key === TAG_NONE) kept = key;
    else if (Number.isInteger(tagIdOf(key))) {
      if (!live || live.has(tagIdOf(key))) kept = key;
      else gone.push(key);
    }
    if (kept !== null && !out.includes(kept)) out.push(kept);
  }
  return out;
}

/* What of a saved payload this case can still honour: the pivot specs, the
   one that was on top, and a line per pivot that lost something. Pure — it
   reads the payload, the live source list and the case's tags, and nothing
   else. */
export function planRestore(payload, sources, tags = null) {
  if (!payload || payload.v !== STATE_VERSION) return null;
  const saved = Array.isArray(payload.pivots) ? payload.pivots.slice(0, MAX_SAVED_PIVOTS) : [];
  if (!saved.length) return null;
  const live = (sources || []).filter((s) => !s.error);   // merges included — invariant #9
  const pivots = [];
  const notes = [];
  for (const p of saved) {
    if (!p || typeof p !== 'object') continue;
    const name = String(p.name || `Pivot ${pivots.length + 1}`);
    const spec = emptySpec(name);
    spec.subtotals = p.subtotals !== false;
    spec.grandTotals = p.grandTotals !== false;
    const src = resolveSource(p.source, live);
    if (!src) {
      // A pivot that never picked a table is not a loss to report — it is
      // a fresh pivot, and fillSources gives it this case's first table.
      if (p.source) {
        const was = p.source.name ? `“${p.source.name}”` : 'its table';
        notes.push(`${name}: ${was} is not in this case any more`);
      }
      pivots.push(spec);
      continue;
    }
    const cols = new Set((src.columns || []).map((c) => c.name));
    const gone = [];
    const kept = (list) => (Array.isArray(list) ? list : []).map(String).filter((n) => {
      if (cols.has(n)) return true;
      gone.push(n);
      return false;
    });
    spec.sourceId = src.id;
    spec.rows = kept(p.rows);
    spec.cols = kept(p.cols);
    spec.values = (Array.isArray(p.values) ? p.values : []).filter((m) => {
      if (!m || typeof m !== 'object' || !m.agg) return false;
      if (!m.column || cols.has(m.column)) return true;   // COUNT(*) names no column
      gone.push(String(m.column));
      return false;
    });
    spec.filters = (Array.isArray(p.filters) ? p.filters : []).filter((f) => {
      if (!f || typeof f !== 'object' || !f.column) return false;
      if (cols.has(f.column)) return true;
      gone.push(String(f.column));
      return false;
    });
    if (gone.length) notes.push(`${name}: dropped ${[...new Set(gone)].join(', ')} — not in ${src.name} any more`);
    const goneTags = [];
    spec.tags = restoreTags(p.tags, tags, goneTags);
    if (goneTags.length) {
      notes.push(`${name}: dropped ${goneTags.length === 1 ? 'a tag' : `${goneTags.length} tags`}`
        + ' from its tag filter — not in this case any more');
    }
    pivots.push(spec);
  }
  if (!pivots.length) return null;
  const a = payload.active;
  const active = Number.isInteger(a) && a >= 0 && a < pivots.length ? a : 0;
  return { pivots, active, notes };
}

let state = null;
let refresh = null;

export default function mount(container, winnow) {
  const { el, post, api, toast, modal } = winnow;

  /* Multiple pivots, SQL-pane style: `pivots` holds one state object per
     tab and `state` is always the ACTIVE one — every render function
     below reads the module-level `state`, so switching tabs is just a
     reassignment plus a re-render. In-memory for the session, like the
     rest of a plugin tab's UI state. */
  let sharedMeta = null;
  const newPivotState = (name) => ({
    name,
    sourceId: null,
    rows: [], cols: [], values: [], filters: [], tags: [],
    meta: sharedMeta, data: null, index: null,
    sort: null,               // {measure: idx, dir: 1|-1} — null = by key
    subtotals: true, grandTotals: true,
    loading: false, error: null, elapsed: 0,
  });
  const pivots = [newPivotState('Pivot 1')];
  let active = 0;
  let renamingIdx = null;
  state = pivots[0];

  /* Nothing is written until the saved pivots have been read back — the
     first render would otherwise save the empty default over them. */
  let saveOn = false;
  /* One untouched pivot is what a fresh tab looks like, so saving it would
     make the next open announce a restore of nothing. Emptying the areas
     is also how an analyst says "forget this", and a null payload is how
     that reaches the case file. */
  const worthKeeping = () => pivots.length > 1 || pivots.some((p) => (
    p.rows.length || p.cols.length || p.values.length || p.filters.length || p.tags.length
    || !p.subtotals || !p.grandTotals));
  function saveState() {
    if (!saveOn || !winnow.tabState) return;   // an older Winnow has no tabState
    winnow.tabState.set(worthKeeping() ? {
      v: STATE_VERSION,
      active,
      pivots: pivots.map((p) => pivotSpec(p, winnow.state.sources)),
    } : null);
  }
  /* The same save, plus the restore line going away: once something has
     been EDITED, "these came back from last time" has stopped being news.
     Clicking between restored pivots is not an edit and saves silently —
     the banner carries what the restore dropped and the only Start fresh
     there is. */
  function saveEdit() {
    hideBanner();
    saveState();
  }

  /* Restoring silently would be worse than not restoring: the analyst
     needs to know these are from last time, when that was, and how to get
     an empty one back. Above the pivot strip, so it reads before the thing
     it is about. */
  const banner = el('div');
  banner.className = 'pv-restored';
  banner.hidden = true;
  banner.style.cssText = 'display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:7px 10px;'
    + 'background:var(--panel-2);border-left:2px solid var(--accent);'
    + 'border-bottom:1px solid var(--line-2);font-size:12px;flex:0 0 auto';
  container.append(banner);

  // The restore line is news that goes stale; the "could not read" line is
  // about what is happening now — it stays until the tab is rebuilt.
  let bannerSticky = false;
  function hideBanner() {
    if (banner.hidden || bannerSticky) return;
    banner.hidden = true;
    banner.replaceChildren();
  }

  /* The read failed, so what is in the case file is unknown — which is not
     the same as nothing being there, and this tab must not write its empty
     default over pivots it never saw. Saving stays off until the tab is
     built again, and that is worth saying rather than leaving the analyst
     to find out later. */
  function showUnreadableBanner() {
    banner.replaceChildren();
    banner.className = 'pv-restored pv-state-unread';
    banner.append(el('span', null, 'Could not read the pivots saved in this case, so nothing '
      + 'here will be saved this time — close and reopen the tab to try again.'));
    bannerSticky = true;
    banner.hidden = false;
  }

  function showRestoredBanner(savedAt, notes) {
    banner.replaceChildren();
    banner.className = 'pv-restored';
    banner.append(el('span', null, 'Restored the pivots you left open when this case was last closed.'));
    const when = String(savedAt || '').replace('T', ' ').slice(0, 16);
    const stamp = el('span', 'note-status', when ? `saved ${when}` : '');
    stamp.style.cssText = 'margin-left:auto;font-size:11px';
    // No link style exists in this app — a real button, sized to the line.
    const fresh = el('button', 'btn ghost pv-start-fresh', 'Start fresh');
    fresh.style.cssText = 'padding:0 6px;font-size:11px;color:var(--accent)';
    fresh.title = 'Forget the saved pivots and start from an empty one';
    fresh.onclick = startFresh;
    banner.append(stamp, fresh);
    if (notes.length) {
      const why = el('div', 'note-status', notes.join(' · '));
      why.style.cssText = 'flex:1 0 100%;font-size:11px';
      banner.append(why);
    }
    banner.hidden = false;
  }

  async function startFresh() {
    hideBanner();
    if (winnow.tabState) await winnow.tabState.clear();
    pivots.splice(0, pivots.length, newPivotState('Pivot 1'));
    active = 0;
    state = pivots[0];
    state.meta = sharedMeta;
    renderPivotTabs();
    fillSources();
    renderFields();
    renderTagRibbon();
    render();
  }

  /* ---------------------------------------------------------- chrome */

  const bar = el('div');
  bar.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px;'
    + 'border-bottom:1px solid var(--line-2);flex:0 0 auto;background:var(--panel)';

  const srcSel = el('select');
  srcSel.title = 'Which table to pivot';
  srcSel.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);'
    + 'padding:5px 8px;font:inherit;max-width:260px';
  srcSel.onchange = () => selectSource(Number(srcSel.value));

  const subBtn = toggleButton('Subtotals', () => state.subtotals, (v) => { state.subtotals = v; scheduleRefresh(); });
  const gtBtn = toggleButton('Grand totals', () => state.grandTotals, (v) => { state.grandTotals = v; render(); saveEdit(); });
  const copyBtn = el('button', 'btn ghost', 'Copy');
  copyBtn.title = 'Copy the pivot as TSV — paste straight into a spreadsheet';
  copyBtn.onclick = () => copyOut('\t', 'Pivot copied');
  const csvBtn = el('button', 'btn ghost', 'CSV');
  csvBtn.title = 'Download the pivot as a CSV file';
  csvBtn.onclick = () => downloadCsv();
  const clearBtn = el('button', 'btn ghost', 'Clear');
  clearBtn.title = 'Empty every field area, and turn the tag filter off';
  clearBtn.onclick = () => {
    state.rows = []; state.cols = []; state.values = []; state.filters = []; state.tags = [];
    state.sort = null; renderFields(); renderTagRibbon(); scheduleRefresh();
  };
  const status = el('span', 'note-status', '');
  status.style.cssText = 'margin-left:auto;text-align:right';
  bar.append(srcSel, subBtn, gtBtn, copyBtn, csvBtn, clearBtn, status);
  const strip = el('div', 'sql-tabs');
  function renderPivotTabs() {
    strip.replaceChildren();
    pivots.forEach((p, i) => {
      if (i === renamingIdx) {
        const inp = el('input');
        inp.value = p.name;
        inp.style.cssText = 'width:110px;font:inherit;font-size:12px;background:var(--ink);'
          + 'color:var(--text);border:1px solid var(--accent);padding:2px 6px';
        const commit = () => {
          p.name = inp.value.trim() || p.name;
          renamingIdx = null;
          renderPivotTabs();
          saveEdit();   // a rename is not a field change, so scheduleRefresh never sees it
        };
        inp.onkeydown = (e) => {
          if (e.key === 'Enter') commit();
          if (e.key === 'Escape') { renamingIdx = null; renderPivotTabs(); }
          e.stopPropagation();
        };
        inp.onblur = commit;
        strip.append(inp);
        setTimeout(() => { inp.focus(); inp.select(); }, 0);
        return;
      }
      const t = el('button', 'sql-tab', p.name);
      t.setAttribute('aria-selected', String(i === active));
      t.title = 'Double-click to rename';
      t.onclick = () => { if (i !== active) activatePivot(i); };
      t.ondblclick = () => { renamingIdx = i; renderPivotTabs(); };
      if (pivots.length > 1) {
        const x = el('span', null, ' ✕');
        x.style.cssText = 'opacity:.6;margin-left:4px';
        x.title = 'Close this pivot';
        x.onclick = (e) => { e.stopPropagation(); closePivot(i); };
        t.append(x);
      }
      strip.append(t);
    });
    const add = el('button', 'sql-tab', '+');
    add.title = 'New pivot';
    add.onclick = () => {
      pivots.push(newPivotState(`Pivot ${pivots.length + 1}`));
      activatePivot(pivots.length - 1);
    };
    strip.append(add);
  }
  function activatePivot(i) {
    active = i;
    state = pivots[i];
    if (state.sourceId != null) srcSel.value = String(state.sourceId);
    renderPivotTabs();
    fillSources();
    renderFields();
    renderTagRibbon();
    loadTagCounts();
    render();
    // Which pivot is on top is part of what gets restored, and one that
    // was restored has no data until something asks for it. Both silent:
    // looking at another pivot is not an edit.
    saveState();
    if (!state.data && !state.loading) queueQuery();
  }
  function closePivot(i) {
    pivots.splice(i, 1);
    activatePivot(Math.max(0, Math.min(i <= active ? active - (i < active ? 1 : 0) : active, pivots.length - 1)));
  }
  container.append(strip);
  container.append(bar);

  const body = el('div');
  body.style.cssText = 'flex:1 1 auto;min-height:0;display:flex;align-items:stretch';
  container.append(body);

  const side = el('div');
  side.style.cssText = 'flex:0 0 268px;min-width:0;border-right:1px solid var(--line-2);'
    + 'display:flex;flex-direction:column;overflow:auto;background:var(--panel)';
  const main = el('div');
  main.style.cssText = 'flex:1 1 auto;min-width:0;overflow:auto;position:relative';
  body.append(side, main);

  /* ------------------------------------------------------- field list */

  const fieldSearch = el('input');
  fieldSearch.type = 'search';
  fieldSearch.placeholder = 'Find a field…';
  fieldSearch.style.cssText = 'margin:8px;background:var(--ink);color:var(--text);'
    + 'border:1px solid var(--line-2);padding:4px 7px;font:inherit;font-size:12px';
  fieldSearch.oninput = renderFields;
  side.append(sectionLabel('Fields'), fieldSearch);

  const fieldList = el('div');
  fieldList.style.cssText = 'display:flex;flex-direction:column;gap:2px;padding:0 8px 8px;'
    + 'max-height:34%;overflow:auto;flex:0 0 auto';
  side.append(fieldList);

  const zoneWrap = el('div');
  zoneWrap.style.cssText = 'display:flex;flex-direction:column;gap:8px;padding:8px;flex:1 1 auto;min-height:0';
  side.append(zoneWrap);

  const ZONES = [
    ['filters', 'Filters', 'Rows the pivot is computed over'],
    ['cols', 'Columns', 'One column group per distinct value'],
    ['rows', 'Rows', 'One row per distinct value; several fields nest'],
    ['values', 'Values', 'What each cell measures'],
  ];
  const zoneBodies = {};
  // The grid's own .tag-ribbon / .tag-chip classes, so the chips look as
  // they do on the ribbon in every skin and theme with no CSS of ours.
  const tagRibbon = el('div', 'tag-ribbon');
  tagRibbon.style.cssText = 'margin-bottom:2px';
  for (const [id, label, hint] of ZONES) {
    const box = el('div');
    box.style.cssText = 'border:1px dashed var(--line-2);border-radius:var(--radius-sm);'
      + 'padding:6px;display:flex;flex-direction:column;gap:4px;min-height:52px';
    box.title = hint;
    box.dataset.zone = id;   // addressable from tests and the console
    const head = el('div', null, label);
    head.style.cssText = 'font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)';
    const list = el('div');
    list.style.cssText = 'display:flex;flex-direction:column;gap:4px';
    box.append(head);
    // The tag chips head the Filters well: they decide which rows the
    // pivot counts, as the field filters below them do, and a pivot that
    // is only counting Suspicious rows should say so where its filters are.
    if (id === 'filters') box.append(tagRibbon);
    box.append(list);
    zoneBodies[id] = list;
    wireDropTarget(box, id);
    zoneWrap.append(box);
  }

  /* --------------------------------------------------- drag and drop */

  let dragging = null; // {from: 'list'|zone id, name, index}

  function wireDragSource(node, payload) {
    node.draggable = true;
    node.addEventListener('dragstart', (e) => {
      dragging = payload;
      e.dataTransfer.effectAllowed = 'move';
      // Something has to be set or Firefox won't start the drag; the real
      // payload rides in the closure, since dataTransfer isn't readable
      // during dragover in most browsers.
      e.dataTransfer.setData('text/plain', payload.name);
      node.style.opacity = '.4';
    });
    node.addEventListener('dragend', () => { dragging = null; node.style.opacity = ''; });
  }

  function wireDropTarget(box, zone) {
    box.addEventListener('dragover', (e) => {
      if (!dragging) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      box.style.borderColor = 'var(--accent)';
    });
    box.addEventListener('dragleave', () => { box.style.borderColor = ''; });
    box.addEventListener('drop', (e) => {
      e.preventDefault();
      box.style.borderColor = '';
      if (dragging) addField(dragging, zone);
      dragging = null;
    });
  }

  /* A field can be in Rows or Columns once — a second copy would produce a
     grouping key that repeats itself. Values are the exception: "Count of X"
     and "Average of X" are two different measures of the same field, which
     is exactly what Excel allows. */
  function addField(payload, zone) {
    const { from, name, index } = payload;
    if (from === zone) return;              // reordering within a zone isn't a move
    if (from !== 'list') removeAt(from, index);

    if (zone === 'values') {
      state.values.push({ column: name, agg: defaultAgg(name) });
    } else if (zone === 'filters') {
      if (!state.filters.some((f) => f.column === name)) {
        state.filters.push({ column: name, op: 'in', values: [], value: '' });
      }
    } else {
      const list = state[zone];
      if (!list.includes(name)) list.push(name);
      // The same field in Rows and Columns is a cross-tab against itself:
      // every off-diagonal cell is empty. Excel just moves it.
      const other = zone === 'rows' ? 'cols' : 'rows';
      state[other] = state[other].filter((c) => c !== name);
    }
    state.sort = null;
    renderFields();
    scheduleRefresh();
  }

  function removeAt(zone, index) {
    state[zone].splice(index, 1);
    state.sort = null;
  }

  function defaultAgg(name) {
    const col = currentSource()?.columns.find((c) => c.name === name);
    return col && col.type === 'number' ? 'sum' : 'count';
  }

  /* ------------------------------------------------------- rendering */

  function sectionLabel(text) {
    const n = el('div', null, text);
    n.style.cssText = 'font-size:10px;letter-spacing:.08em;text-transform:uppercase;'
      + 'color:var(--dim);padding:8px 8px 0';
    return n;
  }

  function toggleButton(label, get, set) {
    const b = el('button', 'btn ghost', label);
    const paint = () => b.setAttribute('aria-pressed', String(get()));
    b.onclick = () => { set(!get()); paint(); };
    paint();
    return b;
  }

  function chip(text, { onRemove, title } = {}) {
    const c = el('div');
    c.dataset.field = text;
    c.style.cssText = 'display:flex;align-items:center;gap:6px;background:var(--panel-2);'
      + 'border:1px solid var(--line);border-radius:var(--radius-sm);padding:3px 6px;'
      + 'font-size:12px;cursor:grab';
    if (title) c.title = title;
    const label = el('span', null, text);
    label.style.cssText = 'flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
    c.append(label);
    if (onRemove) {
      const x = el('button', 'btn ghost', '✕');
      x.style.cssText = 'padding:0 4px;font-size:11px;line-height:1';
      x.onclick = (e) => { e.stopPropagation(); onRemove(); };
      c.append(x);
    }
    return c;
  }

  function renderFields() {
    const src = currentSource();
    const needle = fieldSearch.value.trim().toLowerCase();
    fieldList.replaceChildren();
    for (const col of (src ? src.columns : [])) {
      if (needle && !col.name.toLowerCase().includes(needle)) continue;
      const c = chip(col.name, { title: `${col.name} — ${col.type} · click to place it, or drag it to an area` });
      const type = el('span', 'count', col.type === 'number' ? '#' : col.type === 'datetime' ? '🕑' : '');
      c.insertBefore(type, c.firstChild);
      wireDragSource(c, { from: 'list', name: col.name });
      wirePlace(c, col.name);
      fieldList.append(c);
    }
    if (!fieldList.children.length) {
      fieldList.append(el('div', 'note-status', src ? 'No field matches that.' : 'Pick a table above.'));
    }

    for (const [zone] of ZONES) {
      const list = zoneBodies[zone];
      list.replaceChildren();
      state[zone].forEach((entry, i) => {
        const node = zone === 'values' ? valueChip(entry, i)
          : zone === 'filters' ? filterChip(entry, i)
            : chip(entry, { onRemove: () => { removeAt(zone, i); renderFields(); scheduleRefresh(); } });
        wireDragSource(node, { from: zone, name: entry.column || entry, index: i });
        list.append(node);
      });
      if (!state[zone].length) {
        const hint = el('div', 'note-status', 'Drag a field here');
        hint.style.fontSize = '11px';
        list.append(hint);
      }
    }
  }

  function valueChip(measure, i) {
    const c = chip('', {
      onRemove: () => { removeAt('values', i); renderFields(); scheduleRefresh(); },
      title: `${measure.column} — ${aggLabel(measure.agg)}`,
    });
    const sel = el('select');
    sel.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);'
      + 'font:inherit;font-size:11px;padding:1px 3px;max-width:110px';
    for (const a of state.meta.aggregations) {
      const o = el('option', null, a.label);
      o.value = a.id;
      sel.append(o);
    }
    sel.value = measure.agg;
    sel.onclick = (e) => e.stopPropagation();
    sel.onchange = () => { measure.agg = sel.value; renderFields(); scheduleRefresh(); };
    c.firstChild.textContent = measure.column;
    c.dataset.field = measure.column;
    c.insertBefore(sel, c.lastChild);
    return c;
  }

  function filterChip(filter, i) {
    const c = chip(filter.column, {
      onRemove: () => { removeAt('filters', i); renderFields(); scheduleRefresh(); },
      title: 'Click to choose what this filter keeps',
    });
    const summary = el('span', 'count', filterSummary(filter));
    c.insertBefore(summary, c.lastChild);
    c.onclick = () => openFilterEditor(filter);
    return c;
  }

  function filterSummary(f) {
    const op = state.meta.operators.find((o) => o.id === f.op);
    if (!op) return '';
    if (op.value_kind === 'none') return op.label;
    if (op.value_kind === 'many') return f.values.length ? `${op.label} ${f.values.length}` : 'all';
    return f.value ? `${op.label} ${f.value}` : 'all';
  }

  /* Excel's filter dropdown: pick the operator, then either a checkbox list
     of the column's real values or a single typed operand. */
  function openFilterEditor(filter) {
    modal(`Filter — ${filter.column}`, (b) => {
      const opSel = el('select');
      opSel.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px 7px;font:inherit';
      for (const o of state.meta.operators) {
        const opt = el('option', null, o.label);
        opt.value = o.id;
        opSel.append(opt);
      }
      opSel.value = filter.op;
      b.append(labelled('Keep rows where', opSel));

      const area = el('div');
      area.style.cssText = 'margin-top:10px';
      b.append(area);

      const apply = el('button', 'btn primary', 'Apply');
      apply.style.marginTop = '12px';
      apply.onclick = () => {
        document.getElementById('modal').hidden = true;
        renderFields();
        scheduleRefresh();
      };
      b.append(apply);

      const paint = async () => {
        filter.op = opSel.value;
        const kind = state.meta.operators.find((o) => o.id === filter.op).value_kind;
        area.replaceChildren();
        if (kind === 'none') {
          area.append(el('p', 'note-status', 'No value needed.'));
          return;
        }
        if (kind === 'one') {
          const inp = el('input');
          inp.value = filter.value || '';
          inp.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px 7px;font:inherit;width:100%';
          inp.oninput = () => { filter.value = inp.value; };
          area.append(labelled('Value', inp));
          return;
        }
        area.append(el('p', 'note-status', 'Reading values…'));
        let res;
        try {
          res = await post(`${winnow.base}/values`, { source_id: state.sourceId, column: filter.column });
        } catch (e) {
          area.replaceChildren(el('p', 'note-status', 'Could not read values: ' + e.message));
          return;
        }
        area.replaceChildren();
        const search = el('input');
        search.type = 'search';
        search.placeholder = 'Find a value…';
        search.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px 7px;font:inherit;width:100%';
        const list = el('div');
        list.style.cssText = 'max-height:44vh;overflow:auto;display:flex;flex-direction:column;gap:2px;margin-top:8px';
        const chosen = new Set(filter.values || []);
        const paintList = () => {
          const needle = search.value.trim().toLowerCase();
          list.replaceChildren();
          for (const v of res.values) {
            const text = v.value == null || v.value === '' ? BLANK : String(v.value);
            if (needle && !text.toLowerCase().includes(needle)) continue;
            const row = el('label');
            row.style.cssText = 'display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11px';
            const cb = el('input');
            cb.type = 'checkbox';
            cb.checked = chosen.has(v.value == null ? '' : String(v.value));
            cb.onchange = () => {
              const key = v.value == null ? '' : String(v.value);
              cb.checked ? chosen.add(key) : chosen.delete(key);
              filter.values = [...chosen];
            };
            const name = el('span', null, text);
            name.style.cssText = 'flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
            row.append(cb, name, el('span', 'count', v.count.toLocaleString()));
            list.append(row);
          }
        };
        search.oninput = paintList;
        const acts = el('div', 'row-actions');
        const all = el('button', 'btn ghost', 'All');
        all.onclick = () => { res.values.forEach((v) => chosen.add(v.value == null ? '' : String(v.value))); filter.values = [...chosen]; paintList(); };
        const none = el('button', 'btn ghost', 'None');
        none.onclick = () => { chosen.clear(); filter.values = []; paintList(); };
        acts.append(all, none);
        if (res.truncated) {
          acts.append(el('span', 'note-status', `showing the ${res.values.length.toLocaleString()} most common`));
        }
        area.append(search, acts, list);
        paintList();
      };
      opSel.onchange = paint;
      paint();
    });
  }

  function labelled(text, control) {
    const wrap = el('div');
    wrap.style.cssText = 'display:flex;flex-direction:column;gap:4px;margin-top:8px';
    const l = el('div', 'note-status', text);
    wrap.append(l, control);
    return wrap;
  }

  /* ----------------------------------------------------- tag filter */

  /* The grid's tag ribbon, in the Filters well: the same chips, the same
     clicks and the same right-click menu (static/js/tags.js), so an analyst
     who knows one knows the other. The chip counts are this TABLE's — the
     grid's are its view's — because the pivot's own answer is the cross-tab
     beside them; the tooltip says which. */

  // How many rows of the pivot's table carry each tag, for the chips.
  // Keyed like the grid's S.tagCountsAll; read again whenever the table
  // changes or the tab comes back into view (rows may have been tagged).
  let tagCounts = {};
  let tagCountsSeq = 0;

  const caseTags = () => winnow.state.tags || [];

  async function loadTagCounts() {
    const id = state.sourceId;
    const seq = ++tagCountsSeq;
    if (id == null) { tagCounts = {}; renderTagRibbon(); return; }
    let res;
    try {
      res = await api(`/api/tags?source_id=${id}`);
    } catch (e) {
      return;   // the chips keep the numbers they had; the filter itself is unaffected
    }
    if (seq !== tagCountsSeq) return;   // a newer table's counts are on the way
    tagCounts = res.counts || {};
    renderTagRibbon();
  }

  function setTagFilter(next) {
    state.tags = next;
    renderTagRibbon();
    scheduleRefresh();
  }

  /* The ribbon's click rules, copied rather than imported (a plugin sees
     the app through `winnow` only): click adds, click again removes, a
     specific tag drops Any tag, and an exclusion stays put except for the
     tag being picked, which can't be both. */
  function toggleTagFilterKey(key) {
    const has = state.tags.includes(key);
    const out = state.tags.filter(isTagExclusion).filter((k) => k !== tagExcludeKey(key));
    let next = state.tags.filter((k) => !isTagExclusion(k) && k !== key);
    if (key === TAG_ANY) next = has ? [] : [TAG_ANY];
    else if (!has) next = [...next.filter((k) => k !== TAG_ANY), key];
    setTagFilter([...next, ...out]);
  }

  // Excluding a tag that was filtered TO swaps it, rather than leaving both
  // on and the pivot empty.
  function toggleTagExclusion(id) {
    const key = tagExcludeKey(id);
    const has = state.tags.includes(key);
    const next = state.tags.filter((k) => k !== key && k !== id);
    setTagFilter(has ? next : [...next, key]);
  }

  function tagChipMenuItems(t) {
    return [
      { label: `Filter to ${t.name}`, checked: state.tags.includes(t.id),
        title: 'Count only rows carrying this tag — the same as clicking the chip',
        onclick: () => toggleTagFilterKey(t.id) },
      { label: `Exclude ${t.name}`, checked: state.tags.includes(tagExcludeKey(t.id)),
        title: 'Leave out every row carrying this tag. Combines with the rest of the tag filter: Suspicious with this one excluded means Suspicious rows that are not also this.',
        onclick: () => toggleTagExclusion(t.id) },
      '-',
      { label: 'Clear the tag filter', disabled: !state.tags.length, onclick: () => setTagFilter([]) },
    ];
  }

  function renderTagRibbon() {
    tagRibbon.replaceChildren();
    for (const t of caseTags()) {
      const chip = el('button', 'tag-chip');
      const on = state.tags.includes(t.id);
      const out = state.tags.includes(tagExcludeKey(t.id));
      chip.dataset.tag = String(t.id);
      chip.setAttribute('aria-pressed', String(on));
      chip.classList.toggle('excluded', out);
      chip.style.color = on || out ? t.color : '';
      const sw = el('span', 'swatch');
      sw.style.background = t.color;
      chip.append(sw, el('span', 'name', t.name));
      const n = tagCounts[t.id] || 0;
      if (n) chip.append(el('span', 'n', n.toLocaleString()));
      // No hotkey badge: the keys tag rows in the grid, and there are no
      // rows to tag here.
      chip.title = out
        ? `Excluded: rows tagged ${t.name} are left out of the pivot. Click to count them again.`
        : `${n.toLocaleString()} row${n === 1 ? '' : 's'} in this table tagged ${t.name}. Click to count only those`
          + ' — chips add up, so two of them mean rows carrying either. Right-click to exclude it instead.';
      chip.onclick = () => (out ? toggleTagExclusion(t.id) : toggleTagFilterKey(t.id));
      chip.oncontextmenu = (e) => {
        e.preventDefault();
        winnow.dropdownMenu(chip, tagChipMenuItems(t));
      };
      tagRibbon.append(chip);
    }
    const any = el('button', 'tag-chip');
    any.dataset.tag = TAG_ANY;
    any.setAttribute('aria-pressed', String(state.tags.includes(TAG_ANY)));
    any.title = 'Rows carrying any tag at all. Picking a specific tag replaces it — it already includes every tag.';
    any.append(el('span', null, 'Any tag'));
    any.onclick = () => toggleTagFilterKey(TAG_ANY);
    const none = el('button', 'tag-chip');
    none.dataset.tag = TAG_NONE;
    none.setAttribute('aria-pressed', String(state.tags.includes(TAG_NONE)));
    none.title = 'Rows with no tag on them — what is left to go through. Adds to any tags picked beside it.';
    none.append(el('span', null, 'No tags'));
    none.onclick = () => toggleTagFilterKey(TAG_NONE);
    tagRibbon.append(any, none);
  }

  /* A tag deleted in the grid while this tab was open: its key would name
     nothing, and the backend refuses it rather than count no rows. Dropped
     from every pivot; returns the pivots that lost one. */
  function dropDeletedTags() {
    const live = new Set(caseTags().map((t) => t.id));
    const changed = new Set();
    for (const p of pivots) {
      const kept = p.tags.filter((k) => k === TAG_ANY || k === TAG_NONE || live.has(tagIdOf(k)));
      if (kept.length === p.tags.length) continue;
      p.tags = kept;
      p.data = null;
      changed.add(p);
    }
    return changed;
  }

  const aggLabel = (id) => (state.meta.aggregations.find((a) => a.id === id) || {}).label || id;
  /* Excel's naming, and for the same reason: three Count measures over three
     different fields are three different columns, and labelling them all
     "Count" makes the header useless. Only a count with no field behind it
     (COUNT(*)) is just "Count". */
  const measureLabel = (m) => (m.column ? `${aggLabel(m.agg)} of ${m.column}` : aggLabel(m.agg));

  /* ------------------------------------------------------ the request */

  let timer = null;
  function scheduleRefresh() {
    saveEdit();
    queueQuery();
  }
  /* The re-run on its own. Switching to a pivot that has not been run has
     to fetch its cross-tab, but nothing about the pivot changed — so it is
     not an edit, and it neither writes nor dismisses the restore line. */
  function queueQuery() {
    clearTimeout(timer);
    timer = setTimeout(runQuery, REFRESH_MS);
  }

  /* Every grouping level the table will draw, deepest first. This is the
     list the backend turns into one GROUP BY each. */
  function groupSets() {
    const sets = [];
    const push = (keys) => {
      const json = JSON.stringify(keys);
      if (!sets.some((s) => JSON.stringify(s) === json)) sets.push(keys);
    };
    const rows = state.rows, cols = state.cols;
    push([...rows, ...cols]);                       // the cells themselves
    if (cols.length) push([...rows]);               // the row-total column
    if (state.subtotals) {
      for (let k = 1; k < rows.length; k++) {       // one per nesting level
        push([...rows.slice(0, k), ...cols]);
        if (cols.length) push(rows.slice(0, k));
      }
    }
    if (rows.length) push([...cols]);               // the grand-total row
    push([]);                                       // the single grand total
    return sets;
  }

  /* Monotonic, for the same reason app.js has rebuildSeq: two aggregations
     can complete out of order, and the slower earlier one would otherwise
     index its results under a field layout that's no longer on screen —
     which renders as an empty table with no error. */
  let querySeq = 0;

  async function runQuery() {
    if (state.sourceId == null) return;
    if (!state.values.length && !state.rows.length && !state.cols.length) {
      state.data = null; render(); return;
    }
    const measures = state.values.length ? state.values : [{ agg: 'count' }];
    const sets = groupSets();
    if (sets.length > state.meta.limits.group_sets) {
      state.error = `Too many nesting levels — ${sets.length} grouping queries, the cap is ${state.meta.limits.group_sets}. Turn subtotals off or use fewer row fields.`;
      state.data = null; render(); return;
    }
    // Only the status line changes while the query runs, and the table on
    // screen is dimmed and made inert until the answer replaces it: it is
    // the previous answer, and a click on one of its cells would open the
    // rows for the NEW field layout. A full render here used to rebuild the
    // whole table before the request even went out — that previous answer
    // under the new layout, mostly an empty table — which on a few thousand
    // rows added a few hundred ms to every edit.
    const drawn = state.data && !state.error && main.querySelector('table');
    state.loading = true; state.error = null;
    if (drawn) {
      paintStatus();
      drawn.inert = true;
      drawn.style.opacity = '0.5';
    } else {
      render();
    }
    const started = performance.now();
    const seq = ++querySeq;
    try {
      const res = await post(`${winnow.base}/aggregate`, {
        source_id: state.sourceId,
        values: measures,
        group_sets: sets,
        filters: state.filters,
        tags: state.tags,
      });
      if (seq !== querySeq) return;   // a newer request already answered
      state.data = res;
      state.elapsed = Math.round(performance.now() - started);
      indexData(res);
    } catch (e) {
      if (seq !== querySeq) return;
      state.data = null;
      state.error = e.message;
    } finally {
      if (seq === querySeq) {
        state.loading = false;
        render();
      }
    }
  }

  /* One map per grouping set: tuple of key values -> measure values. The
     key is JSON so it survives values that contain the separator, which a
     command line or a path will. */
  function indexData(res) {
    const index = new Map();
    for (const set of res.sets) {
      const m = new Map();
      const n = set.keys.length;
      for (const row of set.rows) m.set(JSON.stringify(row.slice(0, n).map(norm)), row.slice(n));
      index.set(JSON.stringify(set.keys), { map: m, truncated: set.truncated });
    }
    state.index = index;
  }

  const norm = (v) => (v == null ? '' : String(v));

  /* The single accessor over every grouping set: the measures for a row
     prefix crossed with a column tuple, or undefined when that combination
     has no rows. */
  function lookup(rowVals, colVals) {
    // `null` means the row-total column — the grouping set with no column
    // fields at all. An empty array does NOT mean that: it's the single
    // column group you get when Columns is empty, which still lives in the
    // rows+cols set. `[]` is truthy in JS, so conflating them silently
    // queried the wrong set and made sorting by Total a no-op.
    const withCols = colVals !== null && colVals !== undefined;
    const keys = [...state.rows.slice(0, rowVals.length), ...(withCols ? state.cols : [])];
    const set = state.index && state.index.get(JSON.stringify(keys));
    if (!set) return undefined;
    return set.map.get(JSON.stringify([...rowVals, ...(withCols ? colVals : [])]));
  }

  /* ------------------------------------------------------ the pivot */

  function rowTree() {
    const leafKeys = JSON.stringify([...state.rows, ...state.cols]);
    const leaf = state.index && state.index.get(leafKeys);
    const root = { key: null, children: new Map(), path: [] };
    if (!leaf) return root;
    for (const tuple of leaf.map.keys()) {
      const vals = JSON.parse(tuple).slice(0, state.rows.length);
      let node = root;
      vals.forEach((v, depth) => {
        if (!node.children.has(v)) {
          node.children.set(v, { key: v, children: new Map(), path: [...node.path, v] });
        }
        node = node.children.get(v);
      });
    }
    return root;
  }

  function colTuples() {
    if (!state.cols.length) return [[]];
    const leaf = state.index && state.index.get(JSON.stringify([...state.rows, ...state.cols]));
    if (!leaf) return [[]];
    const seen = new Map();
    for (const tuple of leaf.map.keys()) {
      const vals = JSON.parse(tuple).slice(state.rows.length);
      seen.set(JSON.stringify(vals), vals);
    }
    // Compared element by element rather than on a joined string: a column
    // value can contain any separator you'd pick (paths and command lines
    // contain most of them), and joining would order two different tuples
    // as if they were one.
    return [...seen.values()].sort((a, b) => {
      for (let i = 0; i < Math.max(a.length, b.length); i++) {
        const c = byKey(String(a[i] ?? ''), String(b[i] ?? ''));
        if (c) return c;
      }
      return 0;
    });
  }

  function sortChildren(node, measures) {
    const kids = [...node.children.values()];
    if (state.sort) {
      const { measure, colVals, dir } = state.sort;
      kids.sort((a, b) => {
        const av = (lookup(a.path, colVals) || [])[measure];
        const bv = (lookup(b.path, colVals) || [])[measure];
        const an = av == null ? -Infinity : Number(av);
        const bn = bv == null ? -Infinity : Number(bv);
        return an === bn ? byText(String(a.key), String(b.key)) : (an - bn) * dir;
      });
    } else {
      kids.sort((a, b) => byKey(String(a.key), String(b.key)));
    }
    return kids;
  }

  function render() {
    main.replaceChildren();
    paintStatus();
    if (state.error) {
      main.append(note(state.error, true));
      return;
    }
    if (!state.data) {
      main.append(note(state.sourceId == null
        ? 'Pick a table to pivot.'
        : 'Drag a field into Rows or Columns, and one into Values.'));
      return;
    }

    const measures = state.data.measures;
    const cols = colTuples();
    const truncatedCols = cols.length > MAX_COLS;
    const shown = truncatedCols ? cols.slice(0, MAX_COLS) : cols;
    const table = el('table');
    table.style.cssText = 'border-collapse:collapse;font-size:12px;white-space:nowrap';

    table.append(buildHead(shown, measures));
    const tbody = el('tbody');
    const root = rowTree();
    if (state.rows.length) emitRows(tbody, root, 0, shown, measures);
    if (state.grandTotals) tbody.append(totalRow('Grand total', [], shown, measures, true));
    table.append(tbody);
    main.append(table);

    if (truncatedCols) {
      main.append(note(`Showing ${MAX_COLS} of ${cols.length.toLocaleString()} column groups — `
        + 'a field with this many values reads better in Rows, or filtered down.'));
    }
    const leaf = state.index.get(JSON.stringify([...state.rows, ...state.cols]));
    if (leaf && leaf.truncated) {
      main.append(note(`Stopped at ${state.meta.limits.groups.toLocaleString()} groups — the pivot is incomplete. Add a filter.`, true));
    }
  }

  /* Clicking a value header sorts every level of the row nesting by that
     measure. Which cell is the handle depends on the layout: with several
     measures each gets its own labelled cell, but with a single measure
     under column fields the only *visible* header for a column is the
     column-value cell itself, so that's what has to be clickable. */
  function attachSort(th, mi, colVals) {
    th.style.cursor = 'pointer';
    th.title = 'Sort rows by this column';
    th.onclick = () => {
      if (!isSortedBy(mi, colVals)) state.sort = { measure: mi, colVals: colVals ?? null, dir: -1 };
      else if (state.sort.dir === -1) state.sort.dir = 1;   // biggest first, then smallest
      else state.sort = null;                               // then back to key order
      // No save: the ordering is deliberately not part of pivotSpec (it
      // indexes into the measures, which a restore may have thinned), so a
      // save here would be a write of bytes identical to the stored ones.
      render();
    };
    if (isSortedBy(mi, colVals)) th.textContent = (th.textContent || '') + (state.sort.dir === -1 ? ' ▾' : ' ▴');
  }

  function buildHead(cols, measures) {
    const thead = el('thead');
    const multi = measures.length > 1 || !state.cols.length;

    state.cols.forEach((field, level) => {
      const tr = el('tr');
      // Blank, like Excel's: the row fields are named once, on the measure
      // row below, which is the row they actually sit under.
      const corner = el('th', null, '');
      corner.colSpan = Math.max(1, state.rows.length);
      corner.style.cssText = headCss() + 'text-align:left';
      tr.append(corner);
      // One header row per Columns field, each cell spanning its children.
      let i = 0;
      while (i < cols.length) {
        const value = cols[i][level];
        let span = 1;
        // JSON, not a joined string — a value containing the separator would
        // merge two distinct column groups under one header cell.
        const prefix = JSON.stringify(cols[i].slice(0, level + 1));
        while (i + span < cols.length
               && JSON.stringify(cols[i + span].slice(0, level + 1)) === prefix) span++;
        const th = el('th', null, value === '' ? BLANK : value);
        th.colSpan = span * (multi ? measures.length : 1);
        th.style.cssText = headCss();
        th.title = `${field}: ${value === '' ? BLANK : value}`;
        if (!multi && level === state.cols.length - 1) attachSort(th, 0, cols[i]);
        tr.append(th);
        i += span;
      }
      if (state.grandTotals && state.cols.length) {
        const th = el('th', null, level === 0 ? 'Total' : '');
        th.colSpan = multi ? measures.length : 1;
        th.style.cssText = headCss() + 'border-left:2px solid var(--line-2)';
        if (!multi && level === state.cols.length - 1) attachSort(th, 0, null);
        tr.append(th);
      }
      thead.append(tr);
    });

    // Measure row: always present when there's more than one measure, or
    // when there are no column fields to carry the label.
    const tr = el('tr');
    state.rows.forEach((r) => {
      const th = el('th', null, r);
      th.style.cssText = headCss() + 'text-align:left';
      tr.append(th);
    });
    if (!state.rows.length) {
      const th = el('th', null, '');
      th.style.cssText = headCss();
      tr.append(th);
    }
    const groups = state.grandTotals && state.cols.length ? [...cols, null] : cols;
    for (const colVals of groups) {
      for (let mi = 0; mi < measures.length; mi++) {
        // With one measure under column fields, this row carries the row-field
        // names only — the column header above already says what each group
        // is, including the total column.
        const th = el('th', null, multi ? measureLabel(measures[mi]) : '');
        th.style.cssText = headCss()
          + (colVals === null ? 'border-left:2px solid var(--line-2);' : '');
        if (multi) attachSort(th, mi, colVals);
        tr.append(th);
      }
    }
    thead.append(tr);
    return thead;
  }

  const isSortedBy = (mi, colVals) => !!state.sort && state.sort.measure === mi
    && JSON.stringify(state.sort.colVals ?? null) === JSON.stringify(colVals ?? null);

  function emitRows(tbody, node, depth, cols, measures) {
    for (const child of sortChildren(node, measures)) {
      const isLeaf = depth === state.rows.length - 1;
      if (isLeaf) {
        tbody.append(dataRow(child, depth, cols, measures, false));
      } else {
        tbody.append(dataRow(child, depth, cols, measures, true));
        emitRows(tbody, child, depth + 1, cols, measures);
        if (state.subtotals) {
          tbody.append(totalRow(`${child.key === '' ? BLANK : child.key} total`, child.path, cols, measures, false, depth));
        }
      }
    }
  }

  function dataRow(node, depth, cols, measures, isGroupHeader) {
    const tr = el('tr');
    state.rows.forEach((_, i) => {
      const td = el('td', null, i === depth ? (node.key === '' ? BLANK : node.key) : '');
      td.style.cssText = cellCss() + 'text-align:left;font-family:var(--mono)'
        + (i === depth && isGroupHeader ? ';font-weight:600' : '');
      if (i === depth) td.title = String(node.key);
      tr.append(td);
    });
    if (isGroupHeader) {
      // The values live on the subtotal row underneath this group.
      const span = (cols.length + (state.grandTotals && state.cols.length ? 1 : 0)) * measures.length;
      const filler = el('td');
      filler.colSpan = Math.max(1, span);
      filler.style.cssText = cellCss();
      tr.append(filler);
      return tr;
    }
    appendMeasures(tr, node.path, cols, measures, false);
    return tr;
  }

  function totalRow(label, rowPath, cols, measures, grand, depth = 0) {
    const tr = el('tr');
    tr.style.background = grand ? 'var(--panel-2)' : 'var(--panel)';
    const th = el('td', null, label);
    th.colSpan = Math.max(1, state.rows.length);
    th.style.cssText = cellCss() + `text-align:left;font-weight:600;padding-left:${8 + depth * 12}px`;
    tr.append(th);
    appendMeasures(tr, rowPath, cols, measures, true);
    return tr;
  }

  function appendMeasures(tr, rowPath, cols, measures, isTotal) {
    const groups = state.grandTotals && state.cols.length ? [...cols, null] : cols;
    for (const colVals of groups) {
      const vals = lookup(rowPath, colVals);
      for (let mi = 0; mi < measures.length; mi++) {
        const v = vals ? vals[mi] : undefined;
        const td = el('td', null, formatValue(v, measures[mi]));
        td.style.cssText = cellCss() + 'text-align:right;font-family:var(--mono)'
          + (isTotal ? ';font-weight:600' : '')
          + (colVals === null ? ';border-left:2px solid var(--line-2)' : '');
        if (v != null && !isTotal) {
          td.style.cursor = 'pointer';
          td.title = 'Click to see the rows behind this cell — right-click to show them in the grid';
          td.onclick = () => showDetail(rowPath, colVals || []);
          td.oncontextmenu = (e) => {
            e.preventDefault();
            const plan = gridFilterFor(rowPath, colVals || []);
            winnow.dropdownMenu(td, [
              { label: 'Show the rows here', onclick: () => showDetail(rowPath, colVals || []) },
              gridMenuItem(plan),
            ]);
          };
        }
        tr.append(td);
      }
    }
  }

  function formatValue(v, measure) {
    if (v == null) return '';
    if (typeof v === 'number') {
      // Averages keep two decimals; counts and sums of integers shouldn't
      // grow a ".00" they never had.
      return Number.isInteger(v) ? wholeNumber.format(v) : twoDecimals.format(v);
    }
    return String(v);
  }

  const headCss = () => 'position:sticky;top:0;background:var(--panel-2);color:var(--dim);'
    + 'border:1px solid var(--line);padding:4px 8px;font-size:11px;letter-spacing:.04em;'
    + 'text-transform:uppercase;z-index:1;';
  const cellCss = () => 'border:1px solid var(--line);padding:3px 8px;';

  function note(text, warn) {
    const n = el('div', 'note-status', text);
    n.style.cssText = 'padding:14px' + (warn ? ';color:var(--danger)' : '');
    return n;
  }

  function paintStatus() {
    if (state.loading) { status.textContent = 'Aggregating…'; return; }
    if (!state.data) { status.textContent = ''; return; }
    const leaf = state.index.get(JSON.stringify([...state.rows, ...state.cols]));
    const groups = leaf ? leaf.map.size : 0;
    status.textContent = `${groups.toLocaleString()} group${groups === 1 ? '' : 's'} · `
      + `${state.data.sets.length} level${state.data.sets.length === 1 ? '' : 's'} · ${state.elapsed} ms`;
  }

  /* -------------------------------------------------------- details */

  /* The rows behind a cell, as the grid's own filters — so they can be
     read, tagged and exported there, beside everything else the grid does.
     The cell's values go into the column filter boxes (the filter bar shows
     them); the Filters area becomes filter-builder conditions; the tag
     chips are already in the ribbon's spelling. Each operator is matched to
     the grid's one that compiles to the same SQL (Store._compile_condition
     and this plugin's _where), so the grid lands on the very rows the cell
     counted — tests/test_plugins.py holds the two to that, operator by
     operator.

     The one the grid can't say the same way is > / < on a column it holds
     as text: the pivot compares numbers there, the grid would compare
     strings. That is refused, by name, rather than showing other rows under
     the same filter. The pivot counts the whole table, so the timeframe is
     turned off on the way (openFiltered says so when it does). */
  function gridFilterFor(rowPath, colVals) {
    const src = currentSource();
    if (!src) return { refused: 'Pick a table first' };
    const typeOf = (name) => ((src.columns || []).find((c) => c.name === name) || {}).type;
    const text = (v) => (v == null ? '' : String(v));
    const pairs = [
      ...rowPath.map((v, i) => ({ column: state.rows[i], value: text(v) })),
      ...colVals.map((v, i) => ({ column: state.cols[i], value: text(v) })),
    ];
    const where = [];
    for (const f of state.filters) {
      const kind = ((state.meta.operators || []).find((o) => o.id === f.op) || {}).value_kind;
      if (kind === 'many') {
        const vals = (f.values || []).map(text);
        if (!vals.length) continue;   // "(All)", as the pivot reads it
        if (f.op === 'in') where.push({ column: f.column, op: 'in', value: vals });
        else {
          for (const v of vals) {
            where.push(v === '' ? { column: f.column, op: 'not_empty' } : { column: f.column, op: 'not_equals', value: v });
          }
        }
      } else if (kind === 'one') {
        const v = text(f.value);
        if (v === '') continue;
        if (f.op === 'gt' || f.op === 'lt') {
          const sign = f.op === 'gt' ? '>' : '<';
          if (typeOf(f.column) !== 'number') {
            return { refused: `The grid compares ${f.column} as text, so “${sign} ${v}” would find other rows there than it counted here` };
          }
          where.push({ column: f.column, op: sign, value: v });
        } else {
          where.push({ column: f.column, op: f.op, value: v });   // contains, not_contains, starts
        }
      } else {
        where.push({ column: f.column, op: f.op });               // empty, not_empty
      }
    }
    return { pairs, where, tags: [...state.tags] };
  }

  function showInGrid(plan) {
    if (plan.refused) { toast(plan.refused, 6000); return; }
    winnow.openFiltered(state.sourceId, plan.pairs, { where: plan.where, tags: plan.tags, clearTimeframe: true });
  }

  function gridMenuItem(plan) {
    return {
      label: 'Show these rows in the grid',
      disabled: !!plan.refused,
      title: plan.refused || 'Open the table filtered to this cell, the Filters area and the tag chips',
      onclick: () => showInGrid(plan),
    };
  }

  async function showDetail(rowPath, colVals) {
    const cell = [
      ...rowPath.map((v, i) => ({ column: state.rows[i], value: v })),
      ...colVals.map((v, i) => ({ column: state.cols[i], value: v })),
    ];
    const label = cell.map((c) => `${c.column}=${c.value === '' ? BLANK : c.value}`).join(' · ');
    const plan = gridFilterFor(rowPath, colVals);
    modal(`Rows behind ${label}`, (b) => {
      // Above the rows, so it is there before they load — and when there
      // are more than the 500 this shows, it is the way to all of them.
      const bar = el('div', 'row-actions');
      bar.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:8px';
      const toGrid = el('button', 'btn', 'Show these rows in the grid');
      toGrid.disabled = !!plan.refused;
      toGrid.title = plan.refused || 'Open the table filtered to this cell, the Filters area and the tag chips';
      toGrid.onclick = () => { winnow.closeModal(); showInGrid(plan); };
      bar.append(toGrid);
      if (plan.refused) bar.append(el('span', 'note-status', plan.refused));
      const body = el('div');
      b.append(bar, body);
      body.append(el('p', 'note-status', 'Reading…'));
      post(`${winnow.base}/detail`, { source_id: state.sourceId, cell, filters: state.filters, tags: state.tags })
        .then((res) => {
          body.replaceChildren();
          if (!res.rows.length) { body.append(el('p', 'note-status', 'No rows.')); return; }
          const wrap = el('div');
          wrap.style.cssText = 'overflow:auto;max-height:60vh';
          const t = el('table');
          t.style.cssText = 'border-collapse:collapse;font-size:11px;font-family:var(--mono);white-space:nowrap';
          const hr = el('tr');
          res.columns.forEach((c) => {
            const th = el('th', null, c);
            th.style.cssText = headCss();
            hr.append(th);
          });
          const thead = el('thead');
          thead.append(hr);
          t.append(thead);
          const tb = el('tbody');
          for (const row of res.rows) {
            const tr = el('tr');
            row.forEach((v) => {
              const td = el('td', null, v == null ? '' : String(v));
              td.style.cssText = cellCss() + 'max-width:340px;overflow:hidden;text-overflow:ellipsis';
              td.title = v == null ? '' : String(v);
              tr.append(td);
            });
            tb.append(tr);
          }
          t.append(tb);
          wrap.append(t);
          body.append(wrap);
          if (res.truncated) {
            body.append(el('p', 'note-status',
              `First ${res.rows.length.toLocaleString()} rows — Show these rows in the grid for all of them.`));
          }
        })
        .catch((e) => { body.replaceChildren(el('p', 'note-status', 'Could not read rows: ' + e.message)); });
    }, { wide: true });
  }

  /* --------------------------------------------------------- export */

  function matrix() {
    const table = main.querySelector('table');
    if (!table) return [];
    return [...table.querySelectorAll('tr')].map((tr) =>
      [...tr.children].flatMap((cell) => {
        const span = cell.colSpan || 1;
        return [cell.textContent, ...Array(span - 1).fill('')];
      }));
  }

  function copyOut(sep, msg) {
    const text = matrix().map((r) => r.join(sep)).join('\n');
    if (!text) { toast('Nothing to copy yet'); return; }
    navigator.clipboard.writeText(text).then(() => toast(msg),
      () => toast('Copy failed — the browser blocked clipboard access', 4000));
  }

  function downloadCsv() {
    const rows = matrix();
    if (!rows.length) { toast('Nothing to export yet'); return; }
    // Same formula-injection guard Winnow's own CSV export uses: these files
    // are opened in Excel by definition.
    const safe = (v) => {
      const s = String(v == null ? '' : v);
      const needsQuote = /[",\n\r]/.test(s) || /^[=+\-@\t\r]/.test(s);
      const body = /^[=+\-@\t\r]/.test(s) ? "'" + s : s;
      return needsQuote ? '"' + body.replace(/"/g, '""') + '"' : body;
    };
    const csv = rows.map((r) => r.map(safe).join(',')).join('\r\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    const src = currentSource();
    a.download = `${(src ? src.name : 'pivot').replace(/\.[^.]+$/, '')}-pivot.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    toast('CSV downloaded');
  }

  /* --------------------------------------------------------- sources */

  const currentSource = () => winnow.state.sources.find((s) => s.id === state.sourceId) || null;

  function fillSources() {
    const real = winnow.state.sources.filter((s) => !s.error); // merges included — invariant #9
    const previous = srcSel.value;
    srcSel.replaceChildren();
    for (const s of real) {
      const o = el('option', null, `${s.name} (${s.row_count.toLocaleString()})`);
      o.value = String(s.id);
      srcSel.append(o);
    }
    if (!real.length) {
      state.sourceId = null;
      return;
    }
    // A restored pivot names its own table, and that beats both the
    // select's last value and whatever the grid happens to be showing —
    // otherwise the first fill after a restore would call selectSource and
    // empty the wells it just filled. (First/Last reads the same way.)
    const keep = state.sourceId != null && real.some((s) => s.id === state.sourceId)
      ? String(state.sourceId)
      : (real.some((s) => String(s.id) === previous) ? previous
        : String(winnow.state.sourceId ?? real[0].id));
    srcSel.value = real.some((s) => String(s.id) === keep) ? keep : String(real[0].id);
    if (state.sourceId !== Number(srcSel.value)) selectSource(Number(srcSel.value));
  }

  function selectSource(id) {
    if (state.sourceId === id) return;
    state.sourceId = id;
    // Fields are per-table: keeping them across a switch would leave a
    // pivot referring to columns the new table doesn't have.
    // The tag filter goes with them, though tags are case-wide: a switch
    // starts the pivot over, as the grid starts each table on its own filter.
    state.rows = []; state.cols = []; state.values = []; state.filters = []; state.tags = [];
    state.sort = null; state.data = null;
    renderFields();
    renderTagRibbon();
    loadTagCounts();
    render();
    saveEdit();   // the table is the pivot's first fact; nothing else fires here
  }

  /* Click-to-place, because drag isn't reachable from a keyboard and is
     fiddly on a laptop trackpad. The field is focusable, and Enter or
     Space opens the same menu a click does. */
  function wirePlace(c, name) {
    c.tabIndex = 0;
    c.setAttribute('role', 'button');
    c.setAttribute('aria-haspopup', 'menu');
    c.onclick = () => placeMenu(c, name);
    c.addEventListener('keydown', (e) => {
      if ((e.key !== 'Enter' && e.key !== ' ') || e.target !== c) return;
      e.preventDefault();   // Space would scroll the rail
      if (!e.repeat) placeMenu(c, name, { fromKey: true });
    });
  }

  /* The areas as a small menu under the field — the app's own dropdown
     (winnow.dropdownMenu), so Escape, an outside click and the arrow keys
     behave as every other menu in Winnow does. It was a modal titled
     "Place <field>", which is a whole dialog for one click and covered the
     wells being arranged. Each item does exactly what dropping the field
     on that area does (addField); the menu only says in advance what
     addField would do anyway: an area the field is already in is a no-op,
     so it is shown greyed with why, and Rows/Columns say when the pick
     moves the field out of the other one. */
  function placeMenu(anchor, name, { fromKey = false } = {}) {
    const labelOf = (zone) => ZONES.find(([id]) => id === zone)[1];
    const items = ZONES.map(([id, label, hint]) => {
      const item = {
        label,
        title: hint,
        onclick: () => {
          addField({ from: 'list', name }, id);
          // renderFields rebuilt the list, so the focused field is a new
          // node; a keyboard pick goes back to it rather than to <body>.
          if (fromKey) refocusField(name);
        },
      };
      if (id === 'filters' && state.filters.some((f) => f.column === name)) {
        Object.assign(item, { disabled: true, desc: 'Already there' });
      } else if (id === 'rows' || id === 'cols') {
        const other = id === 'rows' ? 'cols' : 'rows';
        if (state[id].includes(name)) Object.assign(item, { disabled: true, desc: 'Already there' });
        else if (state[other].includes(name)) item.desc = `Moves it out of ${labelOf(other)}`;
      }
      return item;
    });
    winnow.dropdownMenu(anchor, [{ header: 'Place in' }, ...items]);
  }

  function refocusField(name) {
    if (!document.getElementById('modal').hidden) return;
    const again = [...fieldList.children].find((n) => n.dataset.field === name);
    if (again) again.focus();
  }

  /* Back from the grid, where tags may have been edited, deleted or put on
     rows: the chips and their counts are read again, and a pivot filtered
     on tags is re-run, because the rows it counted may not be the rows
     carrying those tags any more.
     Not before the saved pivots have been read back, though. The tab is
     shown the moment it mounts, and a field list drawn then could take a
     drag that is neither saved (saveOn is still false) nor kept (a
     restore replaces the pivots); the read draws the same things itself
     when it lands. */
  let readBack = false;
  refresh = () => {
    if (!readBack) return;
    fillSources();
    renderFields();
    const dropped = dropDeletedTags();
    renderTagRibbon();
    loadTagCounts();
    // Not an edit, so no saveEdit: the restore line stays, as it does when
    // a restore drops a tag.
    if (dropped.size) saveState();
    for (const p of pivots) if (p !== state && p.tags.length) p.data = null;
    if (dropped.has(state) || state.tags.length) queueQuery();
  };

  (async () => {
    try {
      state.meta = await api(`${winnow.base}/meta`);
    } catch (e) {
      container.append(note('Could not load the pivot plugin backend: ' + e.message, true));
      return;
    }
    sharedMeta = state.meta;
    const saved = winnow.tabState ? await winnow.tabState.get() : null;
    // A read that FAILED is not a mount with nothing saved: the row may be
    // sitting there unread. See saveOn below.
    const unreadable = !!(saved && saved.error);
    const restored = saved && !unreadable
      ? planRestore(saved.payload, winnow.state.sources, winnow.state.tags || null) : null;
    if (restored) {
      pivots.splice(0, pivots.length,
        // The ordering a header click applied is deliberately not restored:
        // it indexes into the measures, and a measure may have just been
        // dropped for naming a column the table no longer has.
        ...restored.pivots.map((spec) => Object.assign(newPivotState(spec.name), spec)));
      active = restored.active;
      state = pivots[active];
      showRestoredBanner(saved.savedAt, restored.notes);
    } else if (unreadable) {
      showUnreadableBanner();
    }
    for (const p of pivots) p.meta = sharedMeta;
    state.meta = sharedMeta;
    renderPivotTabs();
    fillSources();
    renderFields();
    renderTagRibbon();
    loadTagCounts();
    render();
    // Saving only starts once we know what a save would replace.
    saveOn = !unreadable;
    readBack = true;
    // The cross-tab is re-queried against the case as it is now.
    if (restored) runQuery();
  })();
}

/* A table imported while the tab was hidden should show up in the picker,
   and a case switch rebuilds the mount entirely (so there's nothing to
   tear down here). */
export function onShow() {
  if (refresh) refresh();
}
