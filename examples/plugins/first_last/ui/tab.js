/* First/Last tab — group events, keep each group's bookends.

   Pick a table, the columns that define a group, the column that orders it,
   a description template, and the columns to carry; preview shows the first
   few groups' bookends live, and "Create table" lands the full result as a
   normal Winnow source (taggable, Timeline-able, exportable). All styling
   rides Winnow's CSS tokens. */

const REFRESH_MS = 350;

let state = null;
let refresh = null;

export default function mount(container, winnow) {
  const { el, post, api, toast } = winnow;

  state = {
    sourceId: null,
    groupBy: [], carry: [], filters: [], tags: { mode: '', ids: [] },
    sortColumn: null,
    template: '{which} of {count}',
    meta: null, preview: null, error: null, loading: false,
  };

  /* ---------------------------------------------------------- chrome */

  const bar = el('div');
  bar.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px;'
    + 'border-bottom:1px solid var(--line-2);flex:0 0 auto;background:var(--panel)';
  const mkSel = (title) => {
    const sel = el('select');
    sel.title = title;
    sel.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);'
      + 'padding:5px 8px;font:inherit;max-width:240px';
    return sel;
  };
  const srcSel = mkSel('Which table to group');
  srcSel.onchange = () => selectSource(Number(srcSel.value));
  const sortSel = mkSel('The column that orders each group — defines which row is first and which is last');
  sortSel.onchange = () => { state.sortColumn = sortSel.value; schedule(); };
  const status = el('span', 'note-status', '');
  status.style.cssText = 'margin-left:auto;text-align:right';
  bar.append(srcSel, el('span', 'note-status', 'ordered by'), sortSel, status);
  container.append(bar);

  const body = el('div');
  body.style.cssText = 'flex:1 1 auto;min-height:0;display:flex;align-items:stretch';
  container.append(body);

  const side = el('div');
  side.style.cssText = 'flex:0 0 300px;min-width:0;border-right:1px solid var(--line-2);'
    + 'display:flex;flex-direction:column;overflow:auto;background:var(--panel);padding:8px;gap:10px';
  const main = el('div');
  main.style.cssText = 'flex:1 1 auto;min-width:0;overflow:auto;display:flex;flex-direction:column';
  body.append(side, main);

  const label = (text) => {
    const n = el('div', null, text);
    n.style.cssText = 'font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)';
    return n;
  };

  const groupBox = el('div');
  const carryBox = el('div');
  const carryOrder = el('div');
  carryOrder.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px';
  for (const [box, cap] of [[groupBox, 'Group rows on'], [carryBox, 'Columns to include']]) {
    box.style.cssText = 'display:flex;flex-direction:column;gap:2px;max-height:22vh;overflow:auto';
    side.append(label(cap), box);
    if (box === carryBox) side.append(carryOrder);
  }

  side.append(label('Only these rows (filters)'));
  const tagBox = el('div');
  tagBox.style.cssText = 'display:flex;flex-direction:column;gap:2px';
  side.append(tagBox);
  const filterBox = el('div');
  filterBox.style.cssText = 'display:flex;flex-direction:column;gap:4px';
  side.append(filterBox);
  const addFilterSel = mkSel('Add a filter on a column');
  side.append(addFilterSel);

  side.append(label('Description'));
  const tmplInput = el('input');
  tmplInput.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);'
    + 'padding:5px 8px;font:12px var(--mono);width:100%';
  tmplInput.value = state.template;
  tmplInput.oninput = () => { state.template = tmplInput.value; schedule(); };
  side.append(tmplInput);
  const chipRow = el('div');
  chipRow.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px';
  side.append(chipRow);
  side.append(el('div', 'note-status',
    'Free text plus placeholders — {which} is First/Last, {count} the group size, '
    + '{Column} that row’s value. Click a chip to insert it.'));

  /* ------------------------------------------------------- rendering */

  const currentSource = () => winnow.state.sources.find((s) => s.id === state.sourceId) || null;

  function checkbox(text, checked, onchange) {
    const row = el('label');
    row.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer';
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = checked;
    cb.onchange = () => onchange(cb.checked);
    row.append(cb, el('span', null, text));
    return row;
  }

  function renderControls() {
    const src = currentSource();
    const cols = src ? src.columns : [];

    sortSel.replaceChildren();
    for (const c of cols) {
      const o = el('option', null, c.name + (c.type === 'datetime' ? ' 🕑' : ''));
      o.value = c.name;
      sortSel.append(o);
    }
    if (!state.sortColumn || !cols.some((c) => c.name === state.sortColumn)) {
      const dt = cols.find((c) => c.type === 'datetime');
      state.sortColumn = dt ? dt.name : (cols[0] ? cols[0].name : null);
    }
    if (state.sortColumn) sortSel.value = state.sortColumn;

    groupBox.replaceChildren();
    carryBox.replaceChildren();
    for (const c of cols) {
      groupBox.append(checkbox(c.name, state.groupBy.includes(c.name), (on) => {
        state.groupBy = on ? [...state.groupBy, c.name] : state.groupBy.filter((n) => n !== c.name);
        schedule();
      }));
      carryBox.append(checkbox(c.name, state.carry.includes(c.name), (on) => {
        state.carry = on ? [...state.carry, c.name] : state.carry.filter((n) => n !== c.name);
        renderCarryOrder();
        schedule();
      }));
    }

    addFilterSel.replaceChildren();
    const none = el('option', null, '+ add a filter…');
    none.value = '';
    addFilterSel.append(none);
    for (const c of cols) {
      const o = el('option', null, c.name);
      o.value = c.name;
      addFilterSel.append(o);
    }
    renderTagFilter();
    renderCarryOrder();
    addFilterSel.value = '';
    addFilterSel.onchange = () => {
      if (!addFilterSel.value) return;
      const f = { column: addFilterSel.value, op: 'in', values: [], value: '' };
      state.filters.push(f);
      addFilterSel.value = '';
      renderFilters();
      openFilterEditor(f);
    };
    renderFilters();

    chipRow.replaceChildren();
    const insert = (text) => {
      const at = tmplInput.selectionStart ?? tmplInput.value.length;
      tmplInput.value = tmplInput.value.slice(0, at) + text + tmplInput.value.slice(tmplInput.selectionEnd ?? at);
      state.template = tmplInput.value;
      tmplInput.focus();
      schedule();
    };
    for (const ph of ['{which}', '{count}', ...cols.map((c) => `{${c.name}}`)]) {
      const chip = el('button', 'btn ghost', ph);
      chip.style.cssText = 'font-size:10px;padding:1px 5px;font-family:var(--mono)';
      chip.onclick = () => insert(ph);
      chipRow.append(chip);
    }
  }

  /* The included columns again, as chips in RESULT order — state.carry is
     what the backend turns into the output header, so the chips are the
     one place that order is visible and draggable. */
  let dragIdx = null;
  function renderCarryOrder() {
    carryOrder.replaceChildren();
    if (!state.carry.length) return;
    state.carry.forEach((name, i) => {
      const chip = el('div', null, name);
      chip.style.cssText = 'display:inline-flex;align-items:center;background:var(--panel-2);'
        + 'border:1px solid var(--line);border-radius:var(--radius-sm);padding:2px 7px;'
        + 'font:11px var(--mono);cursor:grab';
      chip.title = 'Drag to change where this column lands in the created table';
      chip.draggable = true;
      chip.ondragstart = (e) => { dragIdx = i; e.dataTransfer.effectAllowed = 'move'; };
      chip.ondragover = (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; };
      chip.ondrop = (e) => {
        e.preventDefault();
        if (dragIdx === null || dragIdx === i) return;
        const [moved] = state.carry.splice(dragIdx, 1);
        state.carry.splice(i, 0, moved);
        dragIdx = null;
        renderCarryOrder();
        schedule();
      };
      chip.ondragend = () => { dragIdx = null; };
      carryOrder.append(chip);
    });
    const hint = el('div', 'note-status', 'result order — drag to rearrange');
    hint.style.cssText = 'font-size:10px;align-self:center';
    carryOrder.append(hint);
  }

  /* Tag filter — its own control rather than a column filter, because tags
     aren't a column: they live in the app's sidecar, and "only what I've
     tagged TA" is the most common way to scope a bookend pass. */
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

  function renderFilters() {
    filterBox.replaceChildren();
    state.filters.forEach((f, i) => {
      const chip = el('div');
      chip.style.cssText = 'display:flex;align-items:center;gap:6px;background:var(--panel-2);'
        + 'border:1px solid var(--line);border-radius:var(--radius-sm);padding:3px 6px;font-size:12px;cursor:pointer';
      chip.title = 'Click to edit this filter';
      const name = el('span', null, f.column);
      name.style.cssText = 'flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
      const x = el('button', 'btn ghost', '✕');
      x.style.cssText = 'padding:0 4px;font-size:11px;line-height:1';
      x.onclick = (e) => { e.stopPropagation(); state.filters.splice(i, 1); renderFilters(); schedule(); };
      chip.append(name, el('span', 'count', filterSummary(f)), x);
      chip.onclick = () => openFilterEditor(f);
      filterBox.append(chip);
    });
    if (!state.filters.length) filterBox.append(el('div', 'note-status', 'No column filters'));
  }

  /* Same modal editor shape the pivot example uses — operator select plus a
     checkbox value list or a typed operand. */
  function openFilterEditor(filter) {
    winnow.modal(`Filter — ${filter.column}`, (b) => {
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
        renderFilters();
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

  /* -------------------------------------------------- preview + create */

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
      template: state.template,
    };
  }

  async function runPreview() {
    if (state.sourceId == null || !state.groupBy.length || !state.sortColumn) {
      state.preview = null;
      state.error = null;
      renderPreview();
      return;
    }
    state.loading = true;
    renderPreview();
    try {
      state.preview = await post(`${winnow.base}/preview`, requestBody());
      state.error = null;
    } catch (e) {
      state.preview = null;
      state.error = e.message;
    }
    state.loading = false;
    renderPreview();
  }

  const headCss = 'position:sticky;top:0;background:var(--panel-2);color:var(--dim);'
    + 'border:1px solid var(--line);padding:4px 8px;font-size:11px;letter-spacing:.04em;text-transform:uppercase;';
  const cellCss = 'border:1px solid var(--line);padding:3px 8px;font-family:var(--mono);font-size:11px;white-space:nowrap;'
    + 'max-width:420px;overflow:hidden;text-overflow:ellipsis';

  function renderPreview() {
    main.replaceChildren();
    const wrap = el('div');
    wrap.style.cssText = 'flex:1 1 auto;overflow:auto;padding:0';
    main.append(wrap);

    status.textContent = state.loading ? 'Previewing…'
      : state.preview ? `${state.preview.total_groups.toLocaleString()} group${state.preview.total_groups === 1 ? '' : 's'}`
      : '';

    if (state.error) {
      wrap.append(note(state.error, true));
    } else if (!state.preview) {
      wrap.append(note('Pick a table, tick at least one Group by column, and choose the ordering column.'));
    } else {
      const cap = el('div', 'note-status',
        `Previewing the first ${state.meta.limits.preview_groups} groups — the created table covers all `
        + `${state.preview.total_groups.toLocaleString()}.`);
      cap.style.padding = '8px 8px 0';
      wrap.append(cap);
      const t = el('table');
      t.style.cssText = 'border-collapse:collapse;margin:8px;white-space:nowrap';
      const thead = el('thead');
      const hr = el('tr');
      for (const c of state.preview.columns) {
        const th = el('th', null, c);
        th.style.cssText = headCss;
        hr.append(th);
      }
      thead.append(hr);
      t.append(thead);
      const tb = el('tbody');
      for (const row of state.preview.rows) {
        const tr = el('tr');
        row.forEach((v, i) => {
          const td = el('td', null, v == null ? '' : String(v));
          td.style.cssText = cellCss;
          td.title = v == null ? '' : String(v);
          tr.append(td);
        });
        tb.append(tr);
      }
      t.append(tb);
      wrap.append(t);
    }

    const acts = el('div');
    acts.style.cssText = 'flex:0 0 auto;display:flex;gap:8px;align-items:center;padding:8px;'
      + 'border-top:1px solid var(--line-2);background:var(--panel)';
    const nameInput = el('input');
    nameInput.placeholder = currentSource() ? `First-Last of ${currentSource().name}` : 'New table name';
    nameInput.style.cssText = 'flex:1 1 auto;background:var(--ink);color:var(--text);'
      + 'border:1px solid var(--line-2);padding:5px 8px;font:inherit';
    const createBtn = el('button', 'btn', 'Create table');
    createBtn.disabled = !state.preview;
    createBtn.onclick = async () => {
      createBtn.disabled = true;
      try {
        const res = await post(`${winnow.base}/create`, { ...requestBody(), name: nameInput.value.trim() });
        toast(`Created "${res.source.name}" · ${res.source.row_count.toLocaleString()} rows`);
        // create is a synchronous ingest with no job record — refresh the
        // app's source list ourselves or the new table is invisible until a
        // reload, then jump to it.
        if (winnow.refreshSources) await winnow.refreshSources();
        winnow.openSource(res.source.id);
      } catch (e) {
        toast('Could not create the table: ' + e.message, 6000);
        createBtn.disabled = false;
      }
    };
    acts.append(nameInput, createBtn);
    main.append(acts);
  }

  function note(text, warn) {
    const n = el('div', 'note-status', text);
    n.style.cssText = 'padding:14px' + (warn ? ';color:var(--danger)' : '');
    return n;
  }

  /* --------------------------------------------------------- sources */

  function fillSources() {
    const real = winnow.state.sources.filter((s) => !s.is_merge && !s.error);
    const previous = srcSel.value;
    srcSel.replaceChildren();
    for (const s of real) {
      const o = el('option', null, `${s.name} (${s.row_count.toLocaleString()})`);
      o.value = String(s.id);
      srcSel.append(o);
    }
    if (!real.length) { state.sourceId = null; return; }
    const keep = real.some((s) => String(s.id) === previous) ? previous
      : String(winnow.state.sourceId ?? real[0].id);
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
    state.sortColumn = null;
    state.preview = null;
    renderControls();
    renderPreview();
  }

  refresh = () => { fillSources(); renderControls(); };

  (async () => {
    try {
      state.meta = await api(`${winnow.base}/meta`);
    } catch (e) {
      container.append(note('Could not load the plugin backend: ' + e.message, true));
      return;
    }
    fillSources();
    renderControls();
    renderPreview();
  })();
}

export function onShow() {
  if (refresh) refresh();
}
