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

let state = null;
let refresh = null;

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
    groupBy: [], carry: [], filters: [], tags: { mode: '', ids: [] }, rowJson: false,
    sortColumn: null,
    template: '{which} of {count}',
    meta: sharedMeta, preview: null, error: null, loading: false,
    selRows: new Set(), selAnchor: null,
  });
  const sheets = [newSheet('Bookends 1')];
  let active = 0;
  let renamingIdx = null;
  state = sheets[0];

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
    active = i;
    state = sheets[i];
    if (state.sourceId != null) srcSel.value = String(state.sourceId);
    renderSheetTabs();
    fillSources();
    renderControls();
    renderPreview();
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
  // Result actions live top-right, like every other tab's bar.
  const copyBtn = el('button', 'btn ghost', 'Copy result');
  copyBtn.title = 'Copy the ENTIRE result (not just the preview) as TSV — paste into a spreadsheet or notes';
  copyBtn.onclick = copyResult;
  const createBtn = el('button', 'btn', 'Create table…');
  createBtn.title = 'Land the full result as a new table in this case — name it, optionally tag it onto the Timeline';
  createBtn.onclick = openCreateModal;
  bar.append(srcSel, status, copyBtn, createBtn);
  container.append(bar);

  const body = el('div');
  body.style.cssText = 'flex:1 1 auto;min-height:0;display:flex;align-items:stretch';
  container.append(body);

  const side = el('div');
  side.style.cssText = 'flex:0 0 280px;min-width:0;border-right:1px solid var(--line-2);'
    + 'display:flex;flex-direction:column;overflow:auto;background:var(--panel)';
  const main = el('div');
  main.style.cssText = 'flex:1 1 auto;min-width:0;overflow:auto;display:flex;flex-direction:column';
  body.append(side, main);

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
  tmplInput.oninput = () => { state.template = tmplInput.value; schedule(); };
  const chipRow = el('div');
  chipRow.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px';
  tmplWrap.append(tmplInput, chipRow, el('div', 'note-status',
    'Free text plus placeholders — {which} is First/Last, {count} the group size, '
    + '{Column} that row’s value. Click a chip to insert it.'));
  side.append(tmplWrap);

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
    tmplInput.value = state.template;
    const insert = (text) => {
      const at = tmplInput.selectionStart ?? tmplInput.value.length;
      tmplInput.value = tmplInput.value.slice(0, at) + text + tmplInput.value.slice(tmplInput.selectionEnd ?? at);
      state.template = tmplInput.value;
      tmplInput.focus();
      schedule();
    };
    for (const ph of ['{which}', '{count}', ...cols.map((c) => `{${c.name}}`)]) {
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
    sel.onchange = () => { state.tags.mode = sel.value; renderTagFilter(); schedule(); };
    tagBox.append(sel);
    if (state.tags.mode !== 'ids') return;
    const tags = winnow.state.tags || [];
    if (!tags.length) { tagBox.append(el('div', 'note-status', 'No tags in this case yet.')); return; }
    for (const t of tags) {
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
      tagBox.append(row);
    }
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
      const apply = el('button', 'btn', 'Apply');
      apply.style.marginTop = '12px';
      apply.onclick = () => {
        document.getElementById('modal').hidden = true;
        renderControls();
        schedule();
      };
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
        const list = el('div');
        list.style.cssText = 'max-height:44vh;overflow:auto;display:flex;flex-direction:column;gap:2px';
        const chosen = new Set(filter.values || []);
        for (const v of res.values) {
          const text = v.value == null || v.value === '' ? '(blank)' : String(v.value);
          const row = el('label');
          row.style.cssText = 'display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11px';
          const cb = el('input');
          cb.type = 'checkbox';
          const key = v.value == null ? '' : String(v.value);
          cb.checked = chosen.has(key);
          cb.onchange = () => { cb.checked ? chosen.add(key) : chosen.delete(key); filter.values = [...chosen]; };
          row.append(cb, el('span', null, text), el('span', 'count', v.count.toLocaleString()));
          list.append(row);
        }
        area.append(list);
      };
      opSel.onchange = paint;
      paint();
    });
  }

  /* -------------------------------------------------- preview + result */

  let timer = null;
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(runPreview, REFRESH_MS);
  }

  function requestBody() {
    return {
      source_id: state.sourceId,
      group_by: state.groupBy,
      sort_column: state.sortColumn,
      columns: state.carry,
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
      renderPreview();
      return;
    }
    state.loading = true;
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
    state.preview.columns.forEach((c, ci) => {
      const th = el('th', null, c);
      th.style.cssText = headCss;
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
      for (const v of row) {
        const td = el('td', null, v == null ? '' : String(v));
        td.style.cssText = cellCss;
        td.title = v == null ? '' : String(v);
        tr.append(td);
      }
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
    state.sourceId = id;
    state.groupBy = [];
    state.carry = [];
    state.filters = [];
    state.tags = { mode: '', ids: [] };
    state.rowJson = false;
    state.sortColumn = null;
    state.preview = null;
    state.selRows = new Set();
    renderControls();
    renderPreview();
  }

  refresh = () => { fillSources(); renderControls(); };

  (async () => {
    try {
      sharedMeta = await api(`${winnow.base}/meta`);
    } catch (e) {
      container.append(note('Could not load the plugin backend: ' + e.message, true));
      return;
    }
    for (const sh of sheets) sh.meta = sharedMeta;
    state.meta = sharedMeta;
    renderSheetTabs();
    fillSources();
    renderControls();
    renderPreview();
  })();
}

export function onShow() {
  if (refresh) refresh();
}
