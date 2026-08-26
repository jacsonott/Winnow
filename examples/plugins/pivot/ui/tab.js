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
    rows: [], cols: [], values: [], filters: [],
    meta: sharedMeta, data: null, index: null,
    sort: null,               // {measure: idx, dir: 1|-1} — null = by key
    subtotals: true, grandTotals: true,
    loading: false, error: null, elapsed: 0,
  });
  const pivots = [newPivotState('Pivot 1')];
  let active = 0;
  let renamingIdx = null;
  state = pivots[0];

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
  const gtBtn = toggleButton('Grand totals', () => state.grandTotals, (v) => { state.grandTotals = v; render(); });
  const copyBtn = el('button', 'btn ghost', 'Copy');
  copyBtn.title = 'Copy the pivot as TSV — paste straight into a spreadsheet';
  copyBtn.onclick = () => copyOut('\t', 'Pivot copied');
  const csvBtn = el('button', 'btn ghost', 'CSV');
  csvBtn.title = 'Download the pivot as a CSV file';
  csvBtn.onclick = () => downloadCsv();
  const clearBtn = el('button', 'btn ghost', 'Clear');
  clearBtn.title = 'Empty every field area';
  clearBtn.onclick = () => {
    state.rows = []; state.cols = []; state.values = []; state.filters = [];
    state.sort = null; renderFields(); scheduleRefresh();
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
    render();
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
    box.append(head, list);
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
      const c = chip(col.name, { title: `${col.name} — ${col.type}` });
      const type = el('span', 'count', col.type === 'number' ? '#' : col.type === 'datetime' ? '🕑' : '');
      c.insertBefore(type, c.firstChild);
      wireDragSource(c, { from: 'list', name: col.name });
      // Click-to-place, because drag isn't reachable from a keyboard and is
      // fiddly on a laptop trackpad.
      c.onclick = () => placeMenu(c, col.name);
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

      const apply = el('button', 'btn', 'Apply');
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

  const aggLabel = (id) => (state.meta.aggregations.find((a) => a.id === id) || {}).label || id;
  /* Excel's naming, and for the same reason: three Count measures over three
     different fields are three different columns, and labelling them all
     "Count" makes the header useless. Only a count with no field behind it
     (COUNT(*)) is just "Count". */
  const measureLabel = (m) => (m.column ? `${aggLabel(m.agg)} of ${m.column}` : aggLabel(m.agg));

  /* ------------------------------------------------------ the request */

  let timer = null;
  function scheduleRefresh() {
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
    state.loading = true; state.error = null; render();
    const started = performance.now();
    const seq = ++querySeq;
    try {
      const res = await post(`${winnow.base}/aggregate`, {
        source_id: state.sourceId,
        values: measures,
        group_sets: sets,
        filters: state.filters,
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
        const c = String(a[i] ?? '').localeCompare(String(b[i] ?? ''), undefined, { numeric: true });
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
        return an === bn ? String(a.key).localeCompare(String(b.key)) : (an - bn) * dir;
      });
    } else {
      kids.sort((a, b) => String(a.key).localeCompare(String(b.key), undefined, { numeric: true }));
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
          td.title = 'Click to see the rows behind this cell';
          td.onclick = () => showDetail(rowPath, colVals || []);
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
      return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
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

  async function showDetail(rowPath, colVals) {
    const cell = [
      ...rowPath.map((v, i) => ({ column: state.rows[i], value: v })),
      ...colVals.map((v, i) => ({ column: state.cols[i], value: v })),
    ];
    const label = cell.map((c) => `${c.column}=${c.value === '' ? BLANK : c.value}`).join(' · ');
    modal(`Rows behind ${label}`, (b) => {
      b.append(el('p', 'note-status', 'Reading…'));
      post(`${winnow.base}/detail`, { source_id: state.sourceId, cell, filters: state.filters })
        .then((res) => {
          b.replaceChildren();
          if (!res.rows.length) { b.append(el('p', 'note-status', 'No rows.')); return; }
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
          b.append(wrap);
          if (res.truncated) {
            b.append(el('p', 'note-status',
              `First ${res.rows.length.toLocaleString()} rows — open the table and filter to see the rest.`));
          }
        })
        .catch((e) => { b.replaceChildren(el('p', 'note-status', 'Could not read rows: ' + e.message)); });
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
    const real = winnow.state.sources.filter((s) => !s.is_merge && !s.error);
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
    const keep = real.some((s) => String(s.id) === previous) ? previous
      : String(winnow.state.sourceId ?? real[0].id);
    srcSel.value = real.some((s) => String(s.id) === keep) ? keep : String(real[0].id);
    if (state.sourceId !== Number(srcSel.value)) selectSource(Number(srcSel.value));
  }

  function selectSource(id) {
    if (state.sourceId === id) return;
    state.sourceId = id;
    // Fields are per-table: keeping them across a switch would leave a
    // pivot referring to columns the new table doesn't have.
    state.rows = []; state.cols = []; state.values = []; state.filters = [];
    state.sort = null; state.data = null;
    renderFields();
    render();
  }

  function placeMenu(anchor, name) {
    modal(`Place ${name}`, (b) => {
      b.append(el('p', 'note-status', 'Drag works too — this is the click-only path.'));
      const acts = el('div', 'row-actions');
      for (const [id, label] of ZONES.map(([i, l]) => [i, l])) {
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

  refresh = () => { fillSources(); renderFields(); };

  (async () => {
    try {
      state.meta = await api(`${winnow.base}/meta`);
    } catch (e) {
      container.append(note('Could not load the pivot plugin backend: ' + e.message, true));
      return;
    }
    sharedMeta = state.meta;
    for (const p of pivots) p.meta = sharedMeta;
    renderPivotTabs();
    fillSources();
    renderFields();
    render();
  })();
}

/* A table imported while the tab was hidden should show up in the picker,
   and a case switch rebuilds the mount entirely (so there's nothing to
   tear down here). */
export function onShow() {
  if (refresh) refresh();
}
