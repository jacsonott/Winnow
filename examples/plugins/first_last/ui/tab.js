/* First/Last tab — group events, keep each group's bookends.

   The pivot tab's interaction model, applied here: drag fields from the
   list into Group rows on / Ordered by / Include columns / Filters (click
   works too — placeMenu is the keyboard/trackpad path), several bookend
   sheets live as sub-tabs so two groupings can be compared without saving
   either, and the result actions sit top-right on the bar. The preview
   table is a working surface, not a printout: drag its included-column
   headers to reorder the output, click/Shift/Ctrl rows to select, Ctrl+C
   copies the selection as TSV. "Copy result" copies the ENTIRE result
   (the backend's rows route — the preview shows only the first few
   groups); "Create table…" lands it as a normal Winnow source and can
   drop a tag on every row, which is exactly what puts bookends on the
   unified Timeline. All styling rides Winnow's CSS tokens. */

const REFRESH_MS = 350;
/* Auto-update: every change to the controls re-runs the preview after a
   short debounce. On a big table that is a lag on every keystroke of the
   description, so it can be turned off — then edits mark the preview
   stale and Refresh runs it. Remembered in localStorage: the analyst who
   turned it off on this machine has the same data tomorrow. */
const AUTO_KEY = 'winnow.firstlast.auto';
/* The rail's width, dragged on its right edge and remembered per machine
   — twenty chips and a long description want more than 280px, a wide
   preview wants less. */
const RAIL_KEY = 'winnow.firstlast.rail';
const RAIL_MIN = 200, RAIL_MAX = 640, RAIL_DEFAULT = 280;
const readRail = () => {
  try { const n = Number(localStorage.getItem(RAIL_KEY)); return n >= RAIL_MIN && n <= RAIL_MAX ? n : RAIL_DEFAULT; }
  catch { return RAIL_DEFAULT; }
};
const writeRail = (w) => { try { localStorage.setItem(RAIL_KEY, String(w)); } catch { /* private mode */ } };
const readAuto = () => { try { return localStorage.getItem(AUTO_KEY) !== '0'; } catch { return true; } };
const writeAuto = (on) => { try { localStorage.setItem(AUTO_KEY, on ? '1' : '0'); } catch { /* private mode */ } };

/* ------------------------------------------------- saved sheets */

/* The sheets survive the case being closed, because they live in the case
   file: winnow.tabState (plugin API 10) is this mount's own row, written
   as the analyst works — there is no teardown callback in the contract, so
   saving on close is not a thing a plugin can do.

   DEFINITIONS ONLY. What is saved is the question — which fields are in
   which zone, the description, the dragged column widths, which sheet was
   on top. The ANSWER is re-run against the case on open: a preview kept
   from three weeks ago is a picture of evidence rather than the evidence,
   and the row it shows may since have been re-imported, filtered out or
   tagged.

   Two things a payload cannot be trusted about, both of them things this
   plugin would otherwise open on an error banner:
   - the table. A dropped source's id is handed straight to the next
     import, so the id is only believed when the name still matches; a
     table re-imported under a new id is found by name instead.
   - the columns. groupBy/carry/sums/filters go into the request
     unvalidated, and the backend's _check_columns answers a missing one
     with a 400 (renderControls only ever re-defaults the sort column), so
     restore drops what no longer resolves and says which. */

const STATE_VERSION = 1;
// A sheet is a few hundred bytes; two dozen of them is far past what
// anyone builds and still nowhere near winnow.tabState's cap.
const MAX_SAVED_SHEETS = 24;
const TAG_MODES = ['', 'any', 'none', 'ids'];

const emptySpec = (name) => ({
  name,
  sourceId: null,
  groupBy: [], carry: [], sums: [], filters: [],
  tags: { mode: '', ids: [] }, rowJson: false,
  sortColumn: null, colWidths: {}, template: '{which} of {count}',
});

/* How a sheet names its table, and it takes two shapes because tables come
   in two kinds.

   A real source's `name` is the imported file's own, and nothing in the app
   edits it (a nickname is a separate field), so it is what makes the id
   believable after SQLite has handed that id to the next import.

   A MERGE has no file behind it, so its name IS its display name and
   Rename this merge rewrites it (Store.set_source_nickname) — checking the
   name there would read a rename as "that table is gone". Its member
   tables are the identity that survives, and they are also what tells it
   apart from a different merge that took its id after a delete. */
function savedSource(src) {
  if (!src) return null;
  const out = { id: src.id, name: src.name };
  if (src.is_merge) out.members = (src.member_source_ids || []).map(Number);
  return out;
}

/* One sheet, cut down to the spec — no preview, no meta, no selection, no
   loading/stale flags. Those describe a moment, not a question. */
export function sheetSpec(sh, sources) {
  const src = (sources || []).find((s) => s.id === sh.sourceId) || null;
  return {
    name: sh.name,
    source: savedSource(src),
    groupBy: sh.groupBy, carry: sh.carry, sums: sh.sums, filters: sh.filters,
    tags: sh.tags, rowJson: sh.rowJson, sortColumn: sh.sortColumn,
    colWidths: sh.colWidths, template: sh.template,
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

/* What of a saved payload this case can still honour: the sheet specs to
   restore, which one was on top, and a line per sheet that lost something.
   Pure — it reads the payload, the live source list and the case's tags and
   nothing else, which is what makes the id-reuse, dead-column and
   deleted-tag cases testable. */
export function planRestore(payload, sources, tags) {
  if (!payload || payload.v !== STATE_VERSION) return null;
  const saved = Array.isArray(payload.sheets) ? payload.sheets.slice(0, MAX_SAVED_SHEETS) : [];
  if (!saved.length) return null;
  const live = (sources || []).filter((s) => !s.error);   // merges included — invariant #9
  const sheets = [];
  const notes = [];
  for (const sh of saved) {
    if (!sh || typeof sh !== 'object') continue;
    const name = String(sh.name || `Bookends ${sheets.length + 1}`);
    const spec = emptySpec(name);
    if (typeof sh.template === 'string' && sh.template) spec.template = sh.template;
    const src = resolveSource(sh.source, live);
    if (!src) {
      // A sheet that never picked a table is not a loss to report — it is
      // a fresh sheet, and fillSources gives it this case's first table.
      if (sh.source) {
        const was = sh.source.name ? `“${sh.source.name}”` : 'its table';
        notes.push(`${name}: ${was} is not in this case any more`);
      }
      sheets.push(spec);
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
    spec.groupBy = kept(sh.groupBy);
    spec.carry = kept(sh.carry);
    spec.sums = kept(sh.sums);
    spec.filters = (Array.isArray(sh.filters) ? sh.filters : []).filter((f) => {
      if (!f || typeof f !== 'object' || !f.column) return false;
      if (cols.has(f.column)) return true;
      gone.push(String(f.column));
      return false;
    });
    if (typeof sh.sortColumn === 'string' && cols.has(sh.sortColumn)) spec.sortColumn = sh.sortColumn;
    else if (sh.sortColumn) gone.push(String(sh.sortColumn));
    if (sh.tags && typeof sh.tags === 'object') {
      spec.tags = {
        mode: TAG_MODES.includes(sh.tags.mode) ? sh.tags.mode : '',
        ids: (Array.isArray(sh.tags.ids) ? sh.tags.ids : []).filter((n) => typeof n === 'number'),
      };
      // A deleted tag is a column that has gone by another name: "only
      // these tags" against an id nothing carries any more is not an
      // error, it is an empty result, which is the worse failure.
      if (spec.tags.mode === 'ids') {
        const liveTags = new Set((tags || []).map((t) => t.id));
        const before = spec.tags.ids.length;
        spec.tags.ids = spec.tags.ids.filter((id) => liveTags.has(id));
        if (spec.tags.ids.length !== before) {
          if (spec.tags.ids.length) {
            notes.push(`${name}: dropped ${before - spec.tags.ids.length} tag filter(s) this case no longer has`);
          } else {
            spec.tags = { mode: '', ids: [] };
            notes.push(`${name}: the tags it was filtered to are gone — every row is in again`);
          }
        }
      }
    }
    spec.rowJson = !!sh.rowJson;
    // Preview widths are keyed by OUTPUT column, which includes Which /
    // Description / Row (JSON) — not names to look for in the table.
    if (sh.colWidths && typeof sh.colWidths === 'object') {
      for (const c of Object.keys(sh.colWidths)) {
        const w = sh.colWidths[c];
        if (typeof w === 'number' && w > 0) spec.colWidths[c] = w;
      }
    }
    if (gone.length) notes.push(`${name}: dropped ${[...new Set(gone)].join(', ')} — not in ${src.name} any more`);
    sheets.push(spec);
  }
  if (!sheets.length) return null;
  const a = payload.active;
  const active = Number.isInteger(a) && a >= 0 && a < sheets.length ? a : 0;
  return { sheets, active, notes };
}

let state = null;
let refresh = null;
let hideCompletion = null;

export default function mount(container, winnow) {
  const { el, post, api, toast, modal } = winnow;

  /* Multiple sheets, pivot-style: `sheets` holds one state object per
     sub-tab and `state` is always the ACTIVE one — every render function
     reads the module-level `state`, so switching is a reassignment plus a
     re-render. In-memory for the session, like a pivot. */
  let sharedMeta = null;
  const newSheet = (name) => ({
    name,
    sourceId: null,
    groupBy: [], carry: [], sums: [], filters: [], tags: { mode: '', ids: [] }, rowJson: false,
    sortColumn: null,
    colWidths: {},        // preview column name -> px, dragged on the header's edge
    template: '{which} of {count}',
    meta: sharedMeta, preview: null, error: null, loading: false, stale: false,
    selRows: new Set(), selAnchor: null,
  });
  const sheets = [newSheet('Bookends 1')];
  let active = 0;
  let renamingIdx = null;
  state = sheets[0];

  /* Nothing is written until the saved sheets have been read back and
     folded in — the first render would otherwise save the empty default
     over whatever the analyst left here. */
  let saveOn = false;
  /* One untouched sheet is what a fresh tab looks like, so saving it would
     make the next open announce a restore of nothing. Emptying the zones
     is also how an analyst says "forget this", and a null payload is how
     that reaches the case file. */
  const worthKeeping = () => sheets.length > 1 || sheets.some((sh) => (
    sh.groupBy.length || sh.carry.length || sh.sums.length || sh.filters.length
    || sh.tags.mode || sh.rowJson || Object.keys(sh.colWidths).length
    || sh.template !== '{which} of {count}'));
  function saveState() {
    if (!saveOn || !winnow.tabState) return;   // an older Winnow has no tabState
    winnow.tabState.set(worthKeeping() ? {
      v: STATE_VERSION,
      active,
      sheets: sheets.map((sh) => sheetSpec(sh, winnow.state.sources)),
    } : null);
  }
  /* The same save, plus the restore line going away: once something has
     been EDITED, "these came back from last time" has stopped being news.
     Clicking between restored sheets is not an edit and saves silently —
     the banner carries what the restore dropped and the only Start fresh
     there is, and reading it must not be what destroys it. */
  function saveEdit() {
    hideBanner();
    saveState();
  }

  /* ------------------------------------------------------- the banner */

  /* Restoring silently would be worse than not restoring: the analyst
     needs to know these sheets are from last time (and possibly older than
     the evidence), when that was, and how to get an empty one back. Above
     the sheet strip, so it reads before the thing it is about. */
  const banner = el('div');
  banner.className = 'fl-restored';
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
     default over sheets it never saw. Saving stays off until the tab is
     built again, and that is worth saying out loud rather than leaving the
     analyst to find out later. */
  function showUnreadableBanner() {
    banner.replaceChildren();
    banner.className = 'fl-restored fl-state-unread';
    banner.append(el('span', null, 'Could not read the sheets saved in this case, so nothing '
      + 'here will be saved this time — close and reopen the tab to try again.'));
    bannerSticky = true;
    banner.hidden = false;
  }

  function showRestoredBanner(savedAt, notes) {
    banner.replaceChildren();
    banner.className = 'fl-restored';
    banner.append(el('span', null, 'Restored the sheets you left open when this case was last closed.'));
    const when = String(savedAt || '').replace('T', ' ').slice(0, 16);
    const stamp = el('span', 'note-status', when ? `saved ${when}` : '');
    stamp.style.cssText = 'margin-left:auto;font-size:11px';
    // No link style exists in this app (see the .btn / .btn.ghost pair in
    // static/style.css) — a real button, sized down to sit in the line.
    const fresh = el('button', 'btn ghost fl-start-fresh', 'Start fresh');
    fresh.style.cssText = 'padding:0 6px;font-size:11px;color:var(--accent)';
    fresh.title = 'Forget the saved sheets and start from an empty one';
    fresh.onclick = startFresh;
    banner.append(stamp, fresh);
    if (notes.length) {
      // Its own row under the sentence — flex-wrap puts it there, and the
      // dropped fields are the part worth reading twice.
      const why = el('div', 'note-status', notes.join(' · '));
      why.style.cssText = 'flex:1 0 100%;font-size:11px';
      banner.append(why);
    }
    banner.hidden = false;
  }

  async function startFresh() {
    hideBanner();
    if (winnow.tabState) await winnow.tabState.clear();
    sheets.splice(0, sheets.length, newSheet('Bookends 1'));
    active = 0;
    state = sheets[0];
    state.meta = sharedMeta;
    renderSheetTabs();
    fillSources();
    renderControls();
    renderPreview();
  }

  /* ------------------------------------------------------- sheet strip */

  const strip = el('div', 'sql-tabs');
  function renderSheetTabs() {
    strip.replaceChildren();
    sheets.forEach((sh, i) => {
      if (i === renamingIdx) {
        const inp = el('input');
        inp.value = sh.name;
        inp.style.cssText = 'width:110px;font:inherit;font-size:12px;background:var(--ink);'
          + 'color:var(--text);border:1px solid var(--accent);padding:2px 6px';
        const commit = () => {
          sh.name = inp.value.trim() || sh.name;
          renamingIdx = null;
          renderSheetTabs();
          saveEdit();   // a rename is not a control change, so schedule() never sees it
        };
        inp.onkeydown = (e) => {
          if (e.key === 'Enter') commit();
          if (e.key === 'Escape') { renamingIdx = null; renderSheetTabs(); }
          e.stopPropagation();
        };
        inp.onblur = commit;
        strip.append(inp);
        setTimeout(() => { inp.focus(); inp.select(); }, 0);
        return;
      }
      const t = el('button', 'sql-tab', sh.name);
      t.setAttribute('aria-selected', String(i === active));
      t.title = 'Double-click to rename';
      t.onclick = () => { if (i !== active) activateSheet(i); };
      t.ondblclick = () => { renamingIdx = i; renderSheetTabs(); };
      if (sheets.length > 1) {
        const x = el('span', null, ' ✕');
        x.style.cssText = 'opacity:.6;margin-left:4px';
        x.title = 'Close this sheet';
        x.onclick = (e) => { e.stopPropagation(); closeSheet(i); };
        t.append(x);
      }
      strip.append(t);
    });
    const add = el('button', 'sql-tab', '+');
    add.title = 'New sheet — another grouping over the same case, side by side';
    add.onclick = () => {
      sheets.push(newSheet(`Bookends ${sheets.length + 1}`));
      activateSheet(sheets.length - 1);
    };
    strip.append(add);
  }
  function activateSheet(i) {
    acHide();   // state swaps under the input — an accept must not land in another sheet
    active = i;
    state = sheets[i];
    if (state.sourceId != null) srcSel.value = String(state.sourceId);
    renderSheetTabs();
    fillSources();
    renderControls();
    renderPreview();
    // Which sheet is on top is part of what gets restored, and a sheet
    // restored but never run has nothing on screen until something asks.
    // Both silent: looking at another sheet is not an edit.
    saveState();
    if (state.stale) queuePreview();
  }
  function closeSheet(i) {
    sheets.splice(i, 1);
    activateSheet(Math.max(0, Math.min(i <= active ? active - (i < active ? 1 : 0) : active, sheets.length - 1)));
  }
  container.append(strip);

  /* ---------------------------------------------------------- chrome */

  const bar = el('div');
  bar.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px;'
    + 'border-bottom:1px solid var(--line-2);flex:0 0 auto;background:var(--panel)';
  const mkSel = (title) => {
    const sel = el('select');
    sel.title = title;
    sel.style.cssText = 'max-width:240px';
    return sel;
  };
  const srcSel = mkSel('Which table to group');
  srcSel.onchange = () => selectSource(Number(srcSel.value));
  const status = el('span', 'note-status', '');
  status.style.cssText = 'margin-left:auto;text-align:right';
  // Auto-update and its manual counterpart sit between the status and
  // the result actions: they're about the preview, not the result.
  let auto = readAuto();
  const autoRow = el('label');
  autoRow.style.cssText = 'display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer;white-space:nowrap';
  const autoCb = el('input');
  autoCb.type = 'checkbox';
  autoCb.className = 'fl-auto';
  autoCb.checked = auto;
  autoCb.onchange = () => {
    auto = autoCb.checked;
    writeAuto(auto);
    // Turning it back on catches up on whatever was edited meanwhile.
    if (auto && state.stale) schedule();
  };
  autoRow.append(autoCb, el('span', null, 'Auto-update'));
  autoRow.title = 'Re-run the preview as you change the controls. Off, changes wait for Refresh — easier on a large table.';
  const refreshBtn = el('button', 'btn ghost fl-refresh', 'Refresh');
  refreshBtn.title = 'Run the preview now';
  refreshBtn.onclick = () => { clearTimeout(timer); runPreview(); };
  // Result actions live top-right, like every other tab's bar.
  const copyBtn = el('button', 'btn ghost', 'Copy result');
  copyBtn.title = 'Copy the ENTIRE result (not just the preview) as TSV — paste into a spreadsheet or notes';
  copyBtn.onclick = copyResult;
  const createBtn = el('button', 'btn', 'Create table…');
  createBtn.title = 'Land the full result as a new table in this case — name it, optionally tag it onto the Timeline';
  createBtn.onclick = openCreateModal;
  bar.append(srcSel, status, autoRow, refreshBtn, copyBtn, createBtn);
  container.append(bar);

  const body = el('div');
  body.style.cssText = 'flex:1 1 auto;min-height:0;display:flex;align-items:stretch';
  container.append(body);

  const side = el('div');
  side.className = 'fl-rail';
  side.style.cssText = `flex:0 0 ${readRail()}px;min-width:0;border-right:1px solid var(--line-2);`
    + 'display:flex;flex-direction:column;overflow:auto;background:var(--panel)';
  // A drag handle on the rail's edge — the app sidebar's .sidebar-resize,
  // done inline like everything else in this plugin: 7px wide, straddling
  // the border, an accent line while hovered or dragged.
  const rail = el('div');
  rail.className = 'fl-rail-resize';
  rail.title = 'Drag to resize the panel · double-click to reset';
  rail.style.cssText = 'flex:0 0 7px;margin:0 -3px;cursor:col-resize;touch-action:none;z-index:2;position:relative';
  const railLine = el('div');
  railLine.style.cssText = 'position:absolute;inset:0 3px;background:transparent';
  rail.append(railLine);
  rail.onmouseenter = () => { railLine.style.background = 'var(--accent-dim)'; };
  rail.onmouseleave = () => { if (!rail.dataset.drag) railLine.style.background = 'transparent'; };
  rail.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const startX = e.clientX, startW = side.getBoundingClientRect().width;
    rail.dataset.drag = '1';
    railLine.style.background = 'var(--accent-dim)';
    rail.setPointerCapture(e.pointerId);
    const move = (ev) => {
      const w = Math.round(Math.max(RAIL_MIN, Math.min(RAIL_MAX, startW + ev.clientX - startX)));
      side.style.flexBasis = `${w}px`;
    };
    const up = () => {
      rail.removeEventListener('pointermove', move);
      rail.removeEventListener('pointerup', up);
      rail.removeEventListener('pointercancel', up);
      delete rail.dataset.drag;
      railLine.style.background = 'transparent';
      writeRail(Math.round(side.getBoundingClientRect().width));
    };
    rail.addEventListener('pointermove', move);
    rail.addEventListener('pointerup', up);
    rail.addEventListener('pointercancel', up);
  });
  rail.ondblclick = () => { side.style.flexBasis = `${RAIL_DEFAULT}px`; writeRail(RAIL_DEFAULT); };
  const main = el('div');
  main.style.cssText = 'flex:1 1 auto;min-width:0;overflow:auto;display:flex;flex-direction:column';
  body.append(side, rail, main);

  /* ------------------------------------------------------- field list */

  const sectionLabel = (text) => {
    const n = el('div', null, text);
    n.style.cssText = 'font-size:10px;letter-spacing:.08em;text-transform:uppercase;'
      + 'color:var(--dim);padding:8px 8px 0';
    return n;
  };

  const fieldSearch = el('input');
  fieldSearch.type = 'search';
  fieldSearch.placeholder = 'Find a field…';
  fieldSearch.style.cssText = 'margin:8px;background:var(--ink);color:var(--text);'
    + 'border:1px solid var(--line-2);padding:4px 7px;font:inherit;font-size:12px';
  fieldSearch.oninput = renderControls;
  side.append(sectionLabel('Fields'), fieldSearch);

  const fieldList = el('div');
  fieldList.style.cssText = 'display:flex;flex-direction:column;gap:2px;padding:0 8px 8px;'
    + 'max-height:30%;overflow:auto;flex:0 0 auto';
  side.append(fieldList);

  const zoneWrap = el('div');
  zoneWrap.style.cssText = 'display:flex;flex-direction:column;gap:8px;padding:8px;flex:0 0 auto';
  side.append(zoneWrap);

  const ZONES = [
    ['groupBy', 'Group rows on', 'What defines a group — Host + User makes one group per session pair'],
    ['sort', 'Ordered by', 'The column that orders each group; first/last are meaningless without one'],
    ['carry', 'Include columns', 'Columns carried into the result — drag chips (or the preview headers) to set their order'],
    ['sums', 'Total up', 'Number columns summed over each WHOLE group — bytes moved in a session, not just the two bookends'],
    ['filters', 'Filters', 'Only rows matching these are grouped'],
  ];
  const zoneBodies = {};
  for (const [id, label, hint] of ZONES) {
    const box = el('div');
    box.style.cssText = 'border:1px dashed var(--line-2);border-radius:var(--radius-sm);'
      + 'padding:6px;display:flex;flex-direction:column;gap:4px;min-height:44px';
    box.title = hint;
    box.dataset.zone = id;   // addressable from tests and the console
    const head = el('div', null, label);
    head.style.cssText = 'font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)';
    const list = el('div');
    list.style.cssText = 'display:flex;flex-direction:column;gap:4px';
    box.append(head, list);
    zoneBodies[id] = list;
    wireDropTarget(box, id);
    zoneWrap.append(box);
  }

  // Tag filter + whole-row JSON + template, below the zones.
  side.append(sectionLabel('Only rows with tags'));
  const tagBox = el('div');
  tagBox.style.cssText = 'display:flex;flex-direction:column;gap:2px;padding:0 8px';
  side.append(tagBox);
  const rowJsonRow = el('label');
  rowJsonRow.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;padding:8px 8px 0';
  const rowJsonCb = el('input');
  rowJsonCb.type = 'checkbox';
  rowJsonCb.onchange = () => { state.rowJson = rowJsonCb.checked; schedule(); };
  rowJsonRow.append(rowJsonCb, el('span', null, 'Add the whole row as a JSON cell'));
  rowJsonRow.title = 'A "Row (JSON)" column holding each bookend\'s entire source row as a JSON object';
  side.append(rowJsonRow);

  side.append(sectionLabel('Description'));
  const tmplWrap = el('div');
  tmplWrap.style.cssText = 'padding:0 8px 8px;display:flex;flex-direction:column;gap:4px';
  const tmplInput = el('input');
  tmplInput.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);'
    + 'padding:5px 8px;font:12px var(--mono);width:100%';
  tmplInput.oninput = () => { state.template = tmplInput.value; schedule(); acRefresh(); };
  const chipRow = el('div');
  chipRow.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px';
  tmplWrap.append(tmplInput, chipRow, el('div', 'note-status',
    'Free text plus placeholders — {which} is First/Last (Only, for a one-row group), {count} the '
    + 'group size, {Column} that row’s value (any column — it need not be grouped or included), '
    + '{sum:Column} / {min:Column} / {max:Column} a Total-up column’s group total, smallest and '
    + 'largest. Click a chip to insert it, or type { in the box to complete a name.'));
  side.append(tmplWrap);

  /* ------------------------------------------- placeholder completion */

  /* Typing `{` in the description offers every name the template can
     resolve — {which}, {count}, the sum:/min:/max: forms of each Total-up
     column, and every column of the table (the backend projects whatever
     the template names, so none has to be grouped or carried first) —
     narrowed by what follows the brace. Hand-rolled the way the SQL
     pane's is: the winnow context exposes no dropdown, and riding the same
     .menu/.sql-ac classes makes it look identical across the five styles.
     It lives in this container rather than document.body so it dies with
     the mount (case switch, plugin reload) and is hidden with the tab —
     .menu is position:fixed, which the rail's overflow does not clip. */
  let acEl = null, acItems = [], acIdx = 0, acFrom = 0;
  function acHide() {
    if (acEl) acEl.remove();
    acEl = null;
    acItems = [];
  }
  hideCompletion = acHide;
  function acCandidates(partial) {
    const needle = partial.toLowerCase();
    const all = [{ name: 'which', kind: 'First/Last' }, { name: 'count', kind: 'group size' }];
    for (const c of state.sums) for (const k of ['sum', 'min', 'max']) all.push({ name: `${k}:${c}`, kind: 'total' });
    const src = currentSource();
    for (const c of (src ? src.columns : [])) {
      // A brace inside a column name cannot be a placeholder at all — the
      // renderer stops at the first `}` — so it is not offered.
      if (!/[{}]/.test(c.name)) all.push({ name: c.name, kind: c.derived ? 'derived' : 'column' });
    }
    return all.filter((it) => it.name.toLowerCase().includes(needle)).slice(0, 12);
  }
  function acPaint() {
    if (!acEl) {
      acEl = el('div', 'menu sql-ac fl-ac');
      container.append(acEl);
    }
    acEl.replaceChildren();
    acItems.forEach((it, i) => {
      const row = el('button', 'menu-item' + (i === acIdx ? ' sql-ac-active' : ''));
      row.append(el('span', null, it.name), el('span', 'count', it.kind));
      // mousedown, not click: a click would blur the input first and the
      // blur handler would take the popup down before the click landed.
      row.onmousedown = (e) => { e.preventDefault(); acAccept(it); };
      acEl.append(row);
    });
    const r = tmplInput.getBoundingClientRect();
    acEl.style.top = Math.min(r.bottom + 2, window.innerHeight - acEl.offsetHeight - 8) + 'px';
    acEl.style.left = Math.min(r.left, window.innerWidth - acEl.offsetWidth - 8) + 'px';
  }
  function acAccept(item) {
    // The chips' two lines, not renderControls(): that rewrites the
    // input's value and drops the caret mid-edit.
    tmplInput.setRangeText(`{${item.name}}`, acFrom, tmplInput.selectionStart, 'end');
    acHide();
    state.template = tmplInput.value;
    schedule();
    tmplInput.focus();
  }
  function acRefresh({ force = false } = {}) {
    const caret = tmplInput.selectionStart;
    const m = /\{([^{}]*)$/.exec(tmplInput.value.slice(0, caret));
    if (!m && !force) { acHide(); return; }
    // An unclosed brace before the caret is the trigger; Ctrl+Space with
    // none inserts a whole {name} at the caret.
    acFrom = m ? caret - m[0].length : caret;
    acItems = acCandidates(m ? m[1] : '');
    acIdx = 0;
    if (!acItems.length) { acHide(); return; }
    acPaint();
  }
  tmplInput.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === ' ') { e.preventDefault(); acRefresh({ force: true }); return; }
    if (!acEl) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); acIdx = (acIdx + 1) % acItems.length; acPaint(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); acIdx = (acIdx + acItems.length - 1) % acItems.length; acPaint(); }
    else if (e.key === 'Tab' || e.key === 'Enter') {
      e.preventDefault();
      e.stopImmediatePropagation();
      acAccept(acItems[acIdx]);
    } else if (e.key === 'Escape') {
      // Consumed here — the app's own Escape would blur the field.
      e.preventDefault();
      e.stopImmediatePropagation();
      acHide();
    } else if (/^(ArrowLeft|ArrowRight|Home|End)$/.test(e.key)) {
      acHide();   // the caret leaves the token the popup was built for
    }
  });
  tmplInput.addEventListener('blur', () => setTimeout(acHide, 150));
  tmplInput.addEventListener('click', acHide);

  /* --------------------------------------------------- drag and drop */

  let dragging = null; // {from: 'list'|zone id|'header', name, index}

  function wireDragSource(node, payload) {
    node.draggable = true;
    node.addEventListener('dragstart', (e) => {
      dragging = payload;
      e.dataTransfer.effectAllowed = 'move';
      // Something must be set or Firefox won't start the drag; the real
      // payload rides in the closure (dataTransfer isn't readable during
      // dragover in most browsers).
      e.dataTransfer.setData('text/plain', payload.name);
      node.style.opacity = '.4';
    });
    node.addEventListener('dragend', () => { dragging = null; node.style.opacity = ''; });
  }

  function wireDropTarget(box, zone) {
    box.addEventListener('dragover', (e) => {
      if (!dragging || dragging.from === 'header') return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      box.style.borderColor = 'var(--accent)';
    });
    box.addEventListener('dragleave', () => { box.style.borderColor = ''; });
    box.addEventListener('drop', (e) => {
      e.preventDefault();
      box.style.borderColor = '';
      if (dragging && dragging.from !== 'header') addField(dragging, zone);
      dragging = null;
    });
  }

  function addField(payload, zone) {
    const { from, name, index } = payload;
    if (from === zone && zone !== 'carry') return; // in-zone reorder is chip-level, carry only
    if (from !== 'list' && from !== zone) removeAt(from, index);

    if (zone === 'sort') {
      state.sortColumn = name;               // a single slot — dropping replaces
    } else if (zone === 'filters') {
      if (!state.filters.some((f) => f.column === name)) {
        const f = { column: name, op: 'in', values: [], value: '' };
        state.filters.push(f);
        renderControls();
        openFilterEditor(f);
        return;
      }
    } else if (zone === 'sums') {
      // Only a number column has a total. Say so rather than adding a chip
      // the server will refuse on the next preview.
      const col = (currentSource()?.columns || []).find((c) => c.name === name);
      if (!col || col.type !== 'number') {
        toast(`${name} is not a number column — nothing to sum`, 4000);
        return;
      }
      if (!state.sums.includes(name)) state.sums.push(name);
    } else if (zone === 'groupBy' || zone === 'carry') {
      const list = state[zone];
      if (from !== zone && !list.includes(name)) list.push(name);
    }
    state.selRows = new Set();
    renderControls();
    schedule();
  }

  function removeAt(zone, index) {
    if (zone === 'sort') { state.sortColumn = null; return; }
    state[zone].splice(index, 1);
  }

  /* ------------------------------------------------------- rendering */

  const currentSource = () => winnow.state.sources.find((s) => s.id === state.sourceId) || null;

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

  function renderControls() {
    const src = currentSource();
    const cols = src ? src.columns : [];
    const needle = fieldSearch.value.trim().toLowerCase();

    // Default the ordering to the first datetime column — the answer is
    // nearly always "by time", and an empty slot blocks the whole preview.
    if (!state.sortColumn || !cols.some((c) => c.name === state.sortColumn)) {
      const dt = cols.find((c) => c.type === 'datetime');
      state.sortColumn = dt ? dt.name : (cols[0] ? cols[0].name : null);
    }

    fieldList.replaceChildren();
    for (const col of cols) {
      if (needle && !col.name.toLowerCase().includes(needle)) continue;
      const c = chip(col.name, { title: `${col.name} — ${col.type}` });
      const type = el('span', 'count', col.type === 'number' ? '#' : col.type === 'datetime' ? '🕑' : '');
      c.insertBefore(type, c.firstChild);
      wireDragSource(c, { from: 'list', name: col.name });
      // Click-to-place: drag isn't reachable from a keyboard and is fiddly
      // on a trackpad.
      c.onclick = () => placeMenu(col.name);
      fieldList.append(c);
    }
    if (!fieldList.children.length) {
      fieldList.append(el('div', 'note-status', src ? 'No field matches that.' : 'Pick a table above.'));
    }

    for (const [zone] of ZONES) {
      const list = zoneBodies[zone];
      list.replaceChildren();
      const entries = zone === 'sort' ? (state.sortColumn ? [state.sortColumn] : [])
        : state[zone];
      entries.forEach((entry, i) => {
        const node = zone === 'filters' ? filterChip(entry, i)
          : chip(entry, {
            onRemove: zone === 'sort' ? undefined : () => { removeAt(zone, i); renderControls(); schedule(); },
            title: zone === 'sort' ? 'The ordering column — drop another field here to replace it' : undefined,
          });
        wireDragSource(node, { from: zone, name: entry.column || entry, index: i });
        if (zone === 'carry') wireCarryChipReorder(node, i);
        list.append(node);
      });
      if (!entries.length) {
        const hint = el('div', 'note-status', zone === 'sort' ? 'Drop the ordering column here' : 'Drag a field here');
        hint.style.fontSize = '11px';
        list.append(hint);
      }
    }

    chipRow.replaceChildren();
    acHide();   // the value is rewritten below; the popup's brace index was for the old one
    tmplInput.value = state.template;
    const insert = (text) => {
      const at = tmplInput.selectionStart ?? tmplInput.value.length;
      tmplInput.value = tmplInput.value.slice(0, at) + text + tmplInput.value.slice(tmplInput.selectionEnd ?? at);
      state.template = tmplInput.value;
      tmplInput.focus();
      schedule();
    };
    // Totals first among the computed ones: a chip per Total-up column,
    // in the colon form that keeps functions and fields from colliding.
    const sumChips = state.sums.flatMap((c) => [`{sum:${c}}`, `{min:${c}}`, `{max:${c}}`]);
    // The column chips follow the field search above, so a wide table is
    // not sixty buttons; typing { in the input completes the same names.
    const colChips = cols.filter((c) => !needle || c.name.toLowerCase().includes(needle)).map((c) => `{${c.name}}`);
    for (const ph of ['{which}', '{count}', ...sumChips, ...colChips]) {
      const chipBtn = el('button', 'btn ghost', ph);
      chipBtn.style.cssText = 'font-size:10px;padding:1px 5px;font-family:var(--mono)';
      chipBtn.onclick = () => insert(ph);
      chipRow.append(chipBtn);
    }

    rowJsonCb.checked = state.rowJson;
    renderTagFilter();
  }

  /* Chips inside Include columns reorder by drag — their order IS the
     output column order (the preview headers drag the same list). */
  function wireCarryChipReorder(node, i) {
    node.addEventListener('dragover', (e) => {
      if (!dragging || dragging.from !== 'carry' || dragging.index === i) return;
      e.preventDefault();
      e.stopPropagation();
      node.style.borderColor = 'var(--accent)';
    });
    node.addEventListener('dragleave', () => { node.style.borderColor = ''; });
    node.addEventListener('drop', (e) => {
      node.style.borderColor = '';
      if (!dragging || dragging.from !== 'carry' || dragging.index === i) return;
      e.preventDefault();
      e.stopPropagation();
      const [moved] = state.carry.splice(dragging.index, 1);
      state.carry.splice(i, 0, moved);
      dragging = null;
      renderControls();
      schedule();
    });
  }

  function placeMenu(name) {
    modal(`Place ${name}`, (b) => {
      b.append(el('p', 'note-status', 'Drag works too — this is the click-only path.'));
      const acts = el('div', 'row-actions');
      for (const [id, label] of ZONES) {
        const btn = el('button', 'btn ghost', label);
        btn.onclick = () => {
          document.getElementById('modal').hidden = true;
          addField({ from: 'list', name }, id);
        };
        acts.append(btn);
      }
      b.append(acts);
    });
  }

  /* Tag filter — its own control rather than a column filter, because tags
     aren't a column and "only what I've tagged TA" is the most common way
     to scope a bookend pass. */
  let tagNeedle = '';
  function renderTagFilter() {
    tagBox.replaceChildren();
    const sel = mkSel('Keep only rows with (or without) tags');
    sel.style.maxWidth = '100%';
    for (const [v, t] of [['', 'Tags: all rows'], ['any', 'Tags: only tagged rows'],
                          ['none', 'Tags: only untagged rows'], ['ids', 'Tags: only these tags…']]) {
      const o = el('option', null, t);
      o.value = v;
      sel.append(o);
    }
    sel.value = state.tags.mode;
    sel.onchange = () => { state.tags.mode = sel.value; tagNeedle = ''; renderTagFilter(); schedule(); };
    tagBox.append(sel);
    if (state.tags.mode !== 'ids') return;
    const tags = winnow.state.tags || [];
    if (!tags.length) { tagBox.append(el('div', 'note-status', 'No tags in this case yet.')); return; }
    // The same search the value list has — a case with forty tags wants it.
    const search = el('input');
    search.type = 'search';
    search.placeholder = 'Find a tag…';
    search.value = tagNeedle;
    search.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);'
      + 'padding:4px 7px;font:inherit;font-size:12px;margin:2px 0';
    const rows = el('div');
    rows.style.cssText = 'display:flex;flex-direction:column;gap:2px';
    const paintRows = () => {
      rows.replaceChildren();
      const needle = tagNeedle.trim().toLowerCase();
      for (const t of tags) {
        if (needle && !t.name.toLowerCase().includes(needle)) continue;
        const row = el('label');
        row.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer';
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = state.tags.ids.includes(t.id);
        cb.onchange = () => {
          state.tags.ids = cb.checked ? [...state.tags.ids, t.id] : state.tags.ids.filter((x) => x !== t.id);
          schedule();
        };
        const dot = el('span');
        dot.style.cssText = `width:10px;height:10px;border-radius:2px;background:${t.color};flex:0 0 auto`;
        row.append(cb, dot, el('span', null, t.name));
        rows.append(row);
      }
      if (!rows.children.length) rows.append(el('div', 'note-status', 'No tag matches that.'));
    };
    search.oninput = () => { tagNeedle = search.value; paintRows(); };
    tagBox.append(search, rows);
    paintRows();
  }

  function filterSummary(f) {
    const op = state.meta.operators.find((o) => o.id === f.op);
    if (!op) return '';
    if (op.value_kind === 'none') return op.label;
    if (op.value_kind === 'many') return f.values.length ? `${op.label} ${f.values.length}` : 'all';
    return f.value ? `${op.label} ${f.value}` : 'all';
  }

  function filterChip(f, i) {
    const c = chip(f.column, {
      onRemove: () => { state.filters.splice(i, 1); renderControls(); schedule(); },
      title: 'Click to edit this filter',
    });
    c.insertBefore(el('span', 'count', filterSummary(f)), c.lastChild);
    c.onclick = () => openFilterEditor(f);
    return c;
  }

  /* Same modal editor shape the pivot example uses — operator select plus a
     checkbox value list or a typed operand. */
  function openFilterEditor(filter) {
    modal(`Filter — ${filter.column}`, (b) => {
      const opSel = mkSel('Keep rows where');
      for (const o of state.meta.operators) {
        const opt = el('option', null, o.label);
        opt.value = o.id;
        opSel.append(opt);
      }
      opSel.value = filter.op;
      b.append(opSel);
      const area = el('div');
      area.style.cssText = 'margin-top:10px';
      b.append(area);
      const applyNow = () => {
        document.getElementById('modal').hidden = true;
        renderControls();
        schedule();
      };
      const apply = el('button', 'btn', 'Apply');
      apply.style.marginTop = '12px';
      apply.onclick = applyNow;
      b.append(apply);

      const paint = async () => {
        filter.op = opSel.value;
        const kind = state.meta.operators.find((o) => o.id === filter.op).value_kind;
        area.replaceChildren();
        if (kind === 'none') { area.append(el('p', 'note-status', 'No value needed.')); return; }
        if (kind === 'one') {
          const inp = el('input');
          inp.value = filter.value || '';
          inp.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px 7px;font:inherit;width:100%';
          inp.oninput = () => { filter.value = inp.value; };
          area.append(inp);
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
        // The pivot editor's shape, copied rather than imported (plugins
        // are standalone): a search over the values, All/None, and a note
        // when the list is the capped most-common set, not every value.
        const search = el('input');
        search.type = 'search';
        search.placeholder = 'Find a value…';
        search.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px 7px;font:inherit;width:100%';
        const list = el('div');
        list.style.cssText = 'max-height:44vh;overflow:auto;display:flex;flex-direction:column;gap:2px;margin-top:8px';
        const chosen = new Set(filter.values || []);
        const keyOf = (v) => (v.value == null ? '' : String(v.value));
        const textOf = (v) => (v.value == null || v.value === '' ? '(blank)' : String(v.value));
        // What the needle leaves is what the list shows AND what All/None
        // act on — the app's own value picker works the same way, and
        // "type jsmith, All, Apply" is the workflow a search is for.
        const shown = () => {
          const needle = search.value.trim().toLowerCase();
          return needle ? res.values.filter((v) => textOf(v).toLowerCase().includes(needle)) : res.values;
        };
        const paintList = () => {
          list.replaceChildren();
          const rows = shown();
          for (const v of rows) {
            const key = keyOf(v);
            const row = el('label');
            row.style.cssText = 'display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11px';
            const cb = el('input');
            cb.type = 'checkbox';
            cb.checked = chosen.has(key);
            cb.onchange = () => { cb.checked ? chosen.add(key) : chosen.delete(key); filter.values = [...chosen]; };
            const name = el('span', null, textOf(v));
            name.style.cssText = 'flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
            row.append(cb, name, el('span', 'count', v.count.toLocaleString()));
            list.append(row);
          }
          if (!rows.length) list.append(el('div', 'note-status', res.values.length ? 'No value matches that.' : 'No values.'));
        };
        search.oninput = paintList;
        search.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); applyNow(); } };
        const acts = el('div', 'row-actions');
        const all = el('button', 'btn ghost', 'All');
        all.title = 'Tick every value listed — with a search, only the matches';
        all.onclick = () => { for (const v of shown()) chosen.add(keyOf(v)); filter.values = [...chosen]; paintList(); };
        const none = el('button', 'btn ghost', 'None');
        none.title = 'Untick every value listed — with a search, only the matches';
        none.onclick = () => { for (const v of shown()) chosen.delete(keyOf(v)); filter.values = [...chosen]; paintList(); };
        acts.append(all, none);
        if (res.truncated) {
          acts.append(el('span', 'note-status', `showing the ${res.values.length.toLocaleString()} most common`));
        }
        area.append(search, acts, list);
        paintList();
        // modal(opts.focus) cannot reach this — the list paints after the fetch.
        search.focus();
      };
      opSel.onchange = paint;
      paint();
    });
  }

  /* -------------------------------------------------- preview + result */

  let timer = null;
  function schedule() {
    // Above the auto-update gate on purpose: with it off, edits still have
    // to be saved — they just do not re-run the preview.
    saveEdit();
    queuePreview();
  }
  /* The re-run on its own. Switching to a sheet that has not been run has
     to fetch its rows, but nothing about the sheet changed — so it is not
     an edit, and it neither writes nor dismisses the restore line. */
  function queuePreview() {
    clearTimeout(timer);
    if (!auto) {
      // Nothing runs until Refresh — but the status says the preview on
      // screen is behind the controls, so it isn't mistaken for current.
      state.stale = true;
      updateStatus();
      return;
    }
    timer = setTimeout(runPreview, REFRESH_MS);
  }

  function requestBody() {
    return {
      source_id: state.sourceId,
      group_by: state.groupBy,
      sort_column: state.sortColumn,
      columns: state.carry,
      sum_columns: state.sums,
      filters: state.filters,
      tags: state.tags.mode ? state.tags : null,
      row_json: state.rowJson,
      template: state.template,
    };
  }

  const ready = () => state.sourceId != null && state.groupBy.length > 0 && !!state.sortColumn;

  async function runPreview() {
    if (!ready()) {
      state.preview = null;
      state.error = null;
      state.stale = false;   // nothing to run, so nothing is pending either
      renderPreview();
      return;
    }
    state.loading = true;
    state.stale = false;
    renderPreview();
    const mine = state;
    try {
      const p = await post(`${winnow.base}/preview`, requestBody());
      mine.preview = p;
      mine.error = null;
    } catch (e) {
      mine.preview = null;
      mine.error = e.message;
    }
    mine.loading = false;
    mine.selRows = new Set();
    mine.selAnchor = null;
    if (mine === state) renderPreview();
  }

  const headCss = 'position:sticky;top:0;background:var(--panel-2);color:var(--dim);'
    + 'border:1px solid var(--line);padding:4px 8px;font-size:11px;letter-spacing:.04em;text-transform:uppercase;';
  const cellCss = 'border:1px solid var(--line);padding:3px 8px;font-family:var(--mono);font-size:11px;white-space:nowrap;'
    + 'max-width:420px;overflow:hidden;text-overflow:ellipsis';

  function paintSelection(tbody) {
    [...tbody.children].forEach((tr, i) => {
      tr.style.background = state.selRows.has(i) ? 'var(--panel-3)' : '';
      tr.style.boxShadow = state.selRows.has(i) ? 'inset 2px 0 0 var(--accent)' : '';
    });
    updateStatus();
  }

  function updateStatus() {
    if (state.loading) { status.textContent = 'Previewing…'; return; }
    if (state.stale) { status.textContent = 'Changed — press Refresh'; return; }
    if (!state.preview) { status.textContent = ''; return; }
    const n = state.preview.total_groups;
    const sel = state.selRows.size;
    status.textContent = `${n.toLocaleString()} group${n === 1 ? '' : 's'}`
      + (sel ? ` · ${sel} row${sel === 1 ? '' : 's'} selected — Ctrl+C copies` : '');
  }

  function renderPreview() {
    main.replaceChildren();
    const wrap = el('div');
    wrap.style.cssText = 'flex:1 1 auto;overflow:auto;padding:0;outline:none';
    wrap.tabIndex = 0;   // so Ctrl+C lands here after a row click
    main.append(wrap);
    updateStatus();

    if (state.error) {
      wrap.append(note(state.error, true));
      return;
    }
    if (!state.preview) {
      wrap.append(note('Drag at least one field into "Group rows on" (the ordering column defaults to the first timestamp).'));
      return;
    }

    const cap = el('div', 'note-status',
      `Previewing the first ${state.meta.limits.preview_groups} groups of `
      + `${state.preview.total_groups.toLocaleString()} — Copy result / Create table cover all of them. `
      + 'Click rows to select (Shift extends, Ctrl toggles); drag included-column headers to reorder.');
    cap.style.padding = '8px 8px 0';
    wrap.append(cap);

    const t = el('table');
    t.style.cssText = 'border-collapse:collapse;margin:8px;white-space:nowrap;user-select:none';
    const thead = el('thead');
    const hr = el('tr');
    const applyWidth = (node, c) => {
      const w = state.colWidths[c];
      if (!w) return;
      node.style.width = `${w}px`;
      node.style.minWidth = `${w}px`;
      node.style.maxWidth = `${w}px`;
    };
    state.preview.columns.forEach((c, ci) => {
      const th = el('th', null, c);
      th.style.cssText = headCss;
      applyWidth(th, c);
      // Resize from the header's right edge. The handle is its own
      // element so a press on it never starts the header's reorder drag,
      // and it stops the pointer event there for the same reason.
      const grip = el('div');
      grip.className = 'fl-col-resize';
      grip.title = 'Drag to resize · double-click to fit';
      grip.style.cssText = 'position:absolute;top:0;right:-3px;bottom:0;width:7px;cursor:col-resize;touch-action:none;z-index:1';
      grip.draggable = false;
      grip.addEventListener('dragstart', (e) => e.preventDefault());
      grip.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();
        const startX = e.clientX, startW = th.getBoundingClientRect().width;
        const wasDraggable = th.draggable;
        th.draggable = false;
        grip.setPointerCapture(e.pointerId);
        grip.style.background = 'var(--accent-dim)';
        const cells = [...t.querySelectorAll('tbody tr')].map((tr) => tr.children[ci]).filter(Boolean);
        const move = (ev) => {
          const w = Math.round(Math.max(40, startW + ev.clientX - startX));
          state.colWidths[c] = w;
          applyWidth(th, c);
          for (const td of cells) applyWidth(td, c);
        };
        const up = () => {
          grip.removeEventListener('pointermove', move);
          grip.removeEventListener('pointerup', up);
          grip.removeEventListener('pointercancel', up);
          grip.style.background = '';
          th.draggable = wasDraggable;
          saveEdit();   // once, at the end of the drag, not per pointermove
        };
        grip.addEventListener('pointermove', move);
        grip.addEventListener('pointerup', up);
        grip.addEventListener('pointercancel', up);
      });
      grip.addEventListener('dblclick', (e) => {
        e.stopPropagation();
        delete state.colWidths[c];
        saveEdit();
        for (const node of [th, ...[...t.querySelectorAll('tbody tr')].map((tr) => tr.children[ci])]) {
          if (node) { node.style.width = ''; node.style.minWidth = ''; node.style.maxWidth = ''; }
        }
      });
      th.append(grip);
      // Included columns sit between the sort column (0) and the trailing
      // JSON/Description columns — exactly indices 1..carry.length.
      const carryIdx = ci - 1;
      if (carryIdx >= 0 && carryIdx < state.carry.length) {
        th.style.cursor = 'grab';
        th.title = 'Drag onto another included column to reorder the output';
        wireDragSource(th, { from: 'header', name: c, index: carryIdx });
        th.addEventListener('dragover', (e) => {
          if (!dragging || dragging.from !== 'header' || dragging.index === carryIdx) return;
          e.preventDefault();
          th.style.background = 'var(--panel-3)';
        });
        th.addEventListener('dragleave', () => { th.style.background = 'var(--panel-2)'; });
        th.addEventListener('drop', (e) => {
          th.style.background = 'var(--panel-2)';
          if (!dragging || dragging.from !== 'header' || dragging.index === carryIdx) return;
          e.preventDefault();
          const [moved] = state.carry.splice(dragging.index, 1);
          state.carry.splice(carryIdx, 0, moved);
          dragging = null;
          renderControls();
          schedule();
        });
      }
      hr.append(th);
    });
    thead.append(hr);
    t.append(thead);

    const tb = el('tbody');
    state.preview.rows.forEach((row, ri) => {
      const tr = el('tr');
      row.forEach((v, ci) => {
        const td = el('td', null, v == null ? '' : String(v));
        td.style.cssText = cellCss;
        td.title = v == null ? '' : String(v);
        applyWidth(td, state.preview.columns[ci]);
        tr.append(td);
      });
      // Table-tab selection semantics: click selects, Shift extends from
      // the anchor, Ctrl/Cmd toggles.
      tr.addEventListener('mousedown', (e) => {
        if (e.shiftKey && state.selAnchor != null) {
          const [a, b2] = [Math.min(state.selAnchor, ri), Math.max(state.selAnchor, ri)];
          state.selRows = new Set(Array.from({ length: b2 - a + 1 }, (_, k) => a + k));
        } else if (e.ctrlKey || e.metaKey) {
          if (state.selRows.has(ri)) state.selRows.delete(ri); else state.selRows.add(ri);
          state.selAnchor = ri;
        } else {
          state.selRows = new Set([ri]);
          state.selAnchor = ri;
        }
        paintSelection(tb);
        wrap.focus();
      });
      tb.append(tr);
    });
    t.append(tb);
    wrap.append(t);
    paintSelection(tb);

    wrap.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'c' && state.selRows.size) {
        e.preventDefault();
        const lines = [...state.selRows].sort((a, b2) => a - b2)
          .map((i) => state.preview.rows[i].map((v) => (v == null ? '' : String(v))).join('\t'));
        navigator.clipboard.writeText(lines.join('\n')).then(
          () => toast(`Copied ${lines.length} row${lines.length === 1 ? '' : 's'}`),
          () => toast('Copy failed — the browser blocked clipboard access', 4000));
      }
    });
  }

  function note(text, warn) {
    const n = el('div', 'note-status', text);
    n.style.cssText = 'padding:14px' + (warn ? ';color:var(--danger)' : '');
    return n;
  }

  /* The whole result (backend rows route), TSV, to the clipboard. */
  async function copyResult() {
    if (!ready()) { toast('Build a grouping first'); return; }
    let res;
    try { res = await post(`${winnow.base}/rows`, requestBody()); }
    catch (e) { toast('Could not compute the result: ' + e.message, 5000); return; }
    const text = [res.columns.join('\t'),
      ...res.rows.map((r) => r.map((v) => (v == null ? '' : String(v))).join('\t'))].join('\n');
    try {
      await navigator.clipboard.writeText(text);
      toast(`Copied ${res.rows.length.toLocaleString()} row${res.rows.length === 1 ? '' : 's'}`
        + (res.truncated ? ' (truncated — Create a table for the full result)' : ''));
    } catch { toast('Copy failed — the browser blocked clipboard access', 4000); }
  }

  /* Create table…: name it, and optionally put the bookends on the unified
     Timeline — the Timeline is every TAGGED row, so "add to timeline" is a
     tag applied to every row of the new table, under its own name. */
  function openCreateModal() {
    if (!ready()) { toast('Build a grouping first'); return; }
    const src = currentSource();
    modal('Create table', (b) => {
      const defaultName = src ? `First-Last of ${src.name}` : 'First-Last';
      const name = el('input', 'confirm-input');
      name.placeholder = defaultName;
      b.append(el('label', null, 'Table name'), name);

      const tlRow = el('label');
      tlRow.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;margin-top:10px';
      const tlCb = el('input');
      tlCb.type = 'checkbox';
      tlRow.append(tlCb, el('span', null, 'Add these rows to the Timeline (tags every row)'));
      b.append(tlRow);
      const tagName = el('input', 'confirm-input');
      tagName.style.display = 'none';
      b.append(tagName);
      tlCb.onchange = () => {
        tagName.style.display = tlCb.checked ? '' : 'none';
        if (tlCb.checked && !tagName.value) tagName.value = name.value.trim() || defaultName;
      };
      b.append(el('p', 'fb-help', 'The Timeline shows every tagged row across the case — the tag '
        + '(created if needed) is what places these bookends on it, and the rail/tag ribbon pick it up too.'));

      const acts = el('div', 'row-actions');
      const go = el('button', 'btn', 'Create table');
      go.onclick = async () => {
        go.disabled = true;
        try {
          const bodyReq = { ...requestBody(), name: name.value.trim() };
          if (tlCb.checked) bodyReq.timeline_tag = tagName.value.trim() || defaultName;
          const res = await post(`${winnow.base}/create`, bodyReq);
          document.getElementById('modal').hidden = true;
          toast(`Created "${res.source.name}" · ${res.source.row_count.toLocaleString()} rows`
            + (res.timeline_tag ? ` · tagged "${res.timeline_tag.name}"` : ''));
          // create is a synchronous ingest with no job record — refresh the
          // app's source list ourselves, then jump to the new table.
          if (winnow.refreshSources) await winnow.refreshSources();
          winnow.openSource(res.source.id);
        } catch (e) {
          toast('Could not create the table: ' + e.message, 6000);
          go.disabled = false;
        }
      };
      acts.append(go);
      b.append(acts);
      setTimeout(() => name.focus(), 0);
    });
  }

  /* --------------------------------------------------------- sources */

  function fillSources() {
    const real = winnow.state.sources.filter((s) => !s.error); // merges included — invariant #9
    const previous = srcSel.value;
    srcSel.replaceChildren();
    for (const s of real) {
      const o = el('option', null, `${s.name} (${s.row_count.toLocaleString()})`);
      o.value = String(s.id);
      srcSel.append(o);
    }
    if (!real.length) { state.sourceId = null; return; }
    const keep = state.sourceId != null && real.some((s) => s.id === state.sourceId)
      ? String(state.sourceId)
      : (real.some((s) => String(s.id) === previous) ? previous : String(winnow.state.sourceId ?? real[0].id));
    srcSel.value = real.some((s) => String(s.id) === keep) ? keep : String(real[0].id);
    if (state.sourceId !== Number(srcSel.value)) selectSource(Number(srcSel.value));
  }

  function selectSource(id) {
    if (state.sourceId === id) return;
    acHide();   // the column set changes under the popup
    state.sourceId = id;
    state.groupBy = [];
    state.carry = [];
    state.sums = [];      // a total from the old table would 400 on the new one
    state.colWidths = {};
    state.filters = [];
    state.tags = { mode: '', ids: [] };
    state.rowJson = false;
    state.sortColumn = null;
    state.preview = null;
    state.stale = false;
    state.selRows = new Set();
    renderControls();
    renderPreview();
    saveEdit();   // the table is the sheet's first fact; nothing else fires here
  }

  refresh = () => { fillSources(); renderControls(); };

  (async () => {
    try {
      sharedMeta = await api(`${winnow.base}/meta`);
    } catch (e) {
      container.append(note('Could not load the plugin backend: ' + e.message, true));
      return;
    }
    const saved = winnow.tabState ? await winnow.tabState.get() : null;
    // A read that FAILED is not a mount with nothing saved: the row may be
    // sitting there unread. See saveOn below.
    const unreadable = !!(saved && saved.error);
    const restored = saved && !unreadable
      ? planRestore(saved.payload, winnow.state.sources, winnow.state.tags) : null;
    if (restored) {
      // Restored sheets start stale: the definitions are back, the rows
      // are not, and the active one runs below.
      sheets.splice(0, sheets.length,
        ...restored.sheets.map((spec) => Object.assign(newSheet(spec.name), spec, { stale: true })));
      active = restored.active;
      state = sheets[active];
      showRestoredBanner(saved.savedAt, restored.notes);
    } else if (unreadable) {
      showUnreadableBanner();
    }
    for (const sh of sheets) sh.meta = sharedMeta;
    state.meta = sharedMeta;
    renderSheetTabs();
    fillSources();
    renderControls();
    renderPreview();
    // Saving only starts once we know what a save would replace.
    saveOn = !unreadable;
    // The rows are re-run against the case as it is NOW. Regardless of
    // Auto-update: this is the restore, not an edit, and restored controls
    // over an empty table read as a broken tab.
    if (restored) runPreview();
  })();
}

export function onShow() {
  if (refresh) refresh();
}

/* Tab switches reach here (Alt+digit works while typing, so the popup can
   be open); the container's `hidden` already hides the popup with it, but
   the accept state must not survive to the next visit. */
export function onHide() {
  if (hideCompletion) hideCompletion();
}
