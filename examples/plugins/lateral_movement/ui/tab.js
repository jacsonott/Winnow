/* Lateral-movement graph tab (v2) — the reference example for a
   plugin-shipped UI. Everything is drawn from Winnow's own CSS tokens
   (read at draw time, so a theme switch just repaints), and the tab
   honours the case timeframe filter the same as any grid.

   Structure: a movement-EVENT panel (pick any number of {source, event}
   pairs — shipped KAPE defaults plus the analyst's saved ones), a
   directed force graph colored by event type, and a brushable timeline
   histogram of when the hops happened. Double-click a node to jump to
   the evidence rows behind it. */

let S = null;      // whole-tab state; survives tab switches until case switch / reload
let ctx = null;    // the winnow context, captured for onShow

const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const EVENT_COLORS = ['#d9a441', '#39a8e8', '#d9534f', '#7c6cf6', '#39e881', '#b8843a', '#e8973a', '#4ac7c7'];

export default function mount(container, winnow) {
  ctx = winnow;
  const { el, post, toast } = winnow;
  S = { defs: { shipped: [], saved: [] }, selections: [], graph: null,
        bucket: 'day', brush: null, sim: null, view: { x: 0, y: 0, k: 1 }, pan: false };

  const link = el('link'); link.rel = 'stylesheet';
  link.href = `${winnow.assets}/ui/tab.css`;
  container.append(link);

  const root = el('div', 'lm-root');
  container.append(root);

  // ---- toolbar -----------------------------------------------------------
  const bar = el('div', 'lm-bar');
  const build = el('button', 'btn', 'Build graph');
  const manage = el('button', 'btn ghost', 'Events…');
  manage.title = 'Add, edit and save movement-event definitions';
  const useTf = el('label', 'lm-toggle');
  const useTfCb = el('input'); useTfCb.type = 'checkbox'; useTfCb.checked = true;
  useTf.append(useTfCb, el('span', null, 'Apply case timeframe'));
  useTf.title = 'Restrict edges to the timeframe filter set on the tables tab';
  const status = el('span', 'note-status', 'Pick one or more movement events below, then Build.');
  bar.append(build, manage, useTf, status);
  root.append(bar);

  // ---- event-selection panel --------------------------------------------
  const panel = el('div', 'lm-events');
  root.append(panel);

  // ---- graph stage -------------------------------------------------------
  const stage = el('div', 'lm-stage');
  const canvas = el('canvas');
  const tip = el('div', 'lm-tip');
  const legend = el('div', 'lm-legend'); legend.style.display = 'none';
  stage.append(canvas, legend, tip);
  root.append(stage);

  // ---- timeline histogram -----------------------------------------------
  const hist = el('div', 'lm-hist'); hist.style.display = 'none';
  const histCanvas = el('canvas');
  const histLabel = el('div', 'lm-hist-label');
  hist.append(histCanvas, histLabel);
  root.append(hist);

  // ===== event definitions & selection ====================================
  function eventsForSource(src) {
    const cols = new Set(src.columns.map((c) => c.name));
    const all = [...S.defs.shipped, ...S.defs.saved];
    return all.filter((d) => (d.requires || [d.src_col, d.dst_col]).every((c) => cols.has(c)));
  }
  const selKey = (sourceId, name) => `${sourceId}::${name}`;

  function renderPanel() {
    panel.replaceChildren();
    const sources = winnow.state.sources.filter((s) => !s.error);
    if (!sources.length) { panel.append(el('div', 'note-status', 'Import a table first.')); return; }
    let offered = 0;
    for (const src of sources) {
      const events = eventsForSource(src);
      if (!events.length) continue;
      offered += events.length;
      panel.append(el('div', 'lm-src-head', `${src.name} · ${src.row_count.toLocaleString()} rows`));
      for (const d of events) {
        const key = selKey(src.id, d.name);
        const row = el('label', 'lm-event');
        const cb = el('input'); cb.type = 'checkbox';
        cb.checked = S.selections.some((s) => s.key === key);
        cb.onchange = () => {
          if (cb.checked) S.selections.push({ key, source_id: src.id, def: d, color: null });
          else S.selections = S.selections.filter((s) => s.key !== key);
          recolor();
        };
        const sw = el('span', 'lm-swatch');
        const cur = S.selections.find((s) => s.key === key);
        sw.style.background = cur ? cur.color : (d.color || 'var(--dim)');
        row.append(cb, sw, el('span', 'lm-event-name', d.name),
                   el('span', 'lm-event-desc', d.description || ''));
        panel.append(row);
      }
    }
    if (!offered) {
      panel.append(el('div', 'note-status',
        'No table here carries the columns any movement event needs. '
        + 'Import EVTX/logon data, or define an event under Events… for the columns you have.'));
    }
  }

  // Each active selection gets a stable legend color, in pick order.
  function recolor() {
    S.selections.forEach((s, i) => { s.color = EVENT_COLORS[i % EVENT_COLORS.length]; });
    renderPanel();
  }

  // ===== build ============================================================
  build.onclick = async () => {
    if (!S.selections.length) { toast('Tick at least one movement event'); return; }
    build.disabled = true; status.textContent = 'Building…';
    const tr = winnow.state.timeRange;
    const time = (useTfCb.checked && tr && tr.enabled && (tr.start || tr.end))
      ? { start: tr.start, end: tr.end } : {};
    try {
      const r = await post(`${winnow.base}/edges`, {
        selections: S.selections.map((s) => ({
          source_id: s.source_id,
          src_col: s.def.src_col, dst_col: s.def.dst_col,
          label_col: s.def.label_col || null, time_col: s.def.time_col || null,
          strip_prefix: !!s.def.strip_prefix,
          conditions: s.def.conditions || [],
        })),
        time,
      });
      S.bucket = r.bucket;
      loadGraph(r.edges);
      const hosts = S.graph.nodes.size;
      status.textContent = `${hosts} host${hosts === 1 ? '' : 's'}, ${r.edges.length} edge${r.edges.length === 1 ? '' : 's'}`
        + (Object.keys(time).length ? ' · timeframe applied' : '')
        + (r.truncated ? ' · top edges only (filter to narrow)' : '');
      paintLegend();
    } catch (e) {
      status.textContent = ''; toast('Graph failed: ' + e.message, 6000);
    }
    build.disabled = false;
  };

  function loadGraph(edges) {
    const w = canvas.clientWidth || 800, h = canvas.clientHeight || 500;
    const nodes = new Map();
    const node = (name) => {
      let n = nodes.get(name);
      if (!n) { n = { name, x: w / 2 + (Math.random() - .5) * w * .6, y: h / 2 + (Math.random() - .5) * h * .6, vx: 0, vy: 0, deg: 0, out: 0 }; nodes.set(name, n); }
      return n;
    };
    const es = edges.map((e) => {
      const a = node(e.src), b = node(e.dst);
      a.deg += e.n; a.out += e.n; b.deg += e.n;
      return { a, b, k: e.k, n: e.n, labels: e.labels, t: e.t };
    });
    const buckets = new Map();
    for (const e of es) {
      if (e.t == null) continue;
      let row = buckets.get(e.t);
      if (!row) { row = new Array(S.selections.length).fill(0); buckets.set(e.t, row); }
      row[e.k] = (row[e.k] || 0) + e.n;
    }
    S.graph = { nodes, edges: es, hist: [...buckets.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1)) };
    S.brush = null;
    hist.style.display = S.graph.hist.length ? 'block' : 'none';
    S.view = { x: 0, y: 0, k: 1 };
    S.sim = { hot: 1, raf: 0, hover: null, drag: null };
    kick();
    drawHist();
  }

  // ===== simulation + draw ===============================================
  function kick() { if (S.sim && !S.sim.raf) S.sim.raf = requestAnimationFrame(tick); }
  function tick() {
    S.sim.raf = 0; step(); draw();
    if (S.sim.hot > 0.02 || S.sim.drag) kick();
  }
  function step() {
    const g = S.graph; if (!g) return;
    const nodes = [...g.nodes.values()];
    const w = canvas.clientWidth, h = canvas.clientHeight;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        const d2 = Math.max(dx * dx + dy * dy, 25);
        const f = (1400 * S.sim.hot) / d2, d = Math.sqrt(d2);
        dx /= d; dy /= d;
        a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
      }
    }
    for (const e of g.edges) {
      const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d - 120) * 0.01 * S.sim.hot;
      e.a.vx += (dx / d) * f; e.a.vy += (dy / d) * f;
      e.b.vx -= (dx / d) * f; e.b.vy -= (dy / d) * f;
    }
    for (const n of nodes) {
      n.vx += (w / 2 - n.x) * 0.002 * S.sim.hot;
      n.vy += (h / 2 - n.y) * 0.002 * S.sim.hot;
      if (S.sim.drag !== n) { n.x += n.vx; n.y += n.vy; }
      n.vx *= 0.85; n.vy *= 0.85;
    }
    S.sim.hot *= 0.985;
  }
  const radius = (n) => Math.min(4 + Math.sqrt(n.deg) * 1.5, 22);
  const edgeShown = (e) => !S.brush || (e.t != null && e.t >= S.brush[0] && e.t <= S.brush[1]);

  function draw() {
    const g = S.graph; if (!g) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
    const c = canvas.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, w, h);
    c.save();
    c.translate(S.view.x, S.view.y); c.scale(S.view.k, S.view.k);
    const line = css('--line-2'), text = css('--text'), dim = css('--dim'),
      accent = css('--accent'), panel3 = css('--panel-3');
    for (const e of g.edges) {
      if (!edgeShown(e)) continue;
      const hi = S.sim.hover && (e.a === S.sim.hover || e.b === S.sim.hover);
      c.strokeStyle = hi ? accent : (S.selections[e.k] ? S.selections[e.k].color : line);
      c.globalAlpha = hi ? 1 : 0.75;
      c.lineWidth = Math.min(1 + Math.log2(e.n), 6);
      c.beginPath(); c.moveTo(e.a.x, e.a.y); c.lineTo(e.b.x, e.b.y); c.stroke();
      c.globalAlpha = 1;
      const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const tx = e.b.x - (dx / d) * (radius(e.b) + 3), ty = e.b.y - (dy / d) * (radius(e.b) + 3);
      const ang = Math.atan2(dy, dx);
      c.fillStyle = hi ? accent : (S.selections[e.k] ? S.selections[e.k].color : dim);
      c.beginPath(); c.moveTo(tx, ty);
      c.lineTo(tx - 7 * Math.cos(ang - .4), ty - 7 * Math.sin(ang - .4));
      c.lineTo(tx - 7 * Math.cos(ang + .4), ty - 7 * Math.sin(ang + .4));
      c.fill();
    }
    const showLabels = g.nodes.size <= 80;
    c.font = `11px ${css('--mono')}`;
    for (const n of g.nodes.values()) {
      const r = radius(n);
      c.fillStyle = panel3;
      c.strokeStyle = n === S.sim.hover ? accent : (n.out ? accent : line);
      c.lineWidth = n === S.sim.hover ? 2 : 1.25;
      c.beginPath(); c.arc(n.x, n.y, r, 0, Math.PI * 2); c.fill(); c.stroke();
      if (showLabels || n === S.sim.hover) {
        c.fillStyle = n === S.sim.hover ? text : dim;
        c.fillText(n.name, n.x + r + 4, n.y + 4);
      }
    }
    c.restore();
  }

  function paintLegend() {
    legend.replaceChildren();
    if (!S.selections.length) { legend.style.display = 'none'; return; }
    legend.style.display = 'block';
    for (const s of S.selections) {
      const row = el('div');
      const sw = el('span', 'lm-swatch'); sw.style.background = s.color;
      row.append(sw, el('span', null, s.def.name));
      legend.append(row);
    }
    legend.append(el('div', 'note-status', 'Double-click a host → its evidence rows'));
  }

  // ===== timeline histogram ===============================================
  function drawHist() {
    const g = S.graph; if (!g || !g.hist.length) return;
    const dpr = window.devicePixelRatio || 1;
    const w = histCanvas.clientWidth, h = histCanvas.clientHeight;
    if (histCanvas.width !== w * dpr || histCanvas.height !== h * dpr) { histCanvas.width = w * dpr; histCanvas.height = h * dpr; }
    const c = histCanvas.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, h);
    const n = g.hist.length, bw = w / n;
    const max = Math.max(1, ...g.hist.map(([, row]) => row.reduce((a, b) => a + b, 0)));
    g.hist.forEach(([bkt, row], i) => {
      let y = h;
      const inBrush = !S.brush || (bkt >= S.brush[0] && bkt <= S.brush[1]);
      row.forEach((v, k) => {
        if (!v) return;
        const bh = (v / max) * (h - 14);
        c.fillStyle = S.selections[k] ? S.selections[k].color : css('--dim');
        c.globalAlpha = inBrush ? 1 : 0.3;
        c.fillRect(i * bw + 1, y - bh, Math.max(1, bw - 2), bh);
        y -= bh;
      });
    });
    c.globalAlpha = 1;
    histLabel.textContent = `${g.hist[0][0]} → ${g.hist[n - 1][0]} · per ${S.bucket}`
      + (S.brush ? ' · brushed (click to clear)' : '');
  }

  // Drag across the histogram to brush a time window; a plain click (no
  // drag) clears an existing brush. brushMoved separates the two — the
  // click event that trails every drag would otherwise clear the brush
  // the drag just set.
  let brushStart = null, brushMoved = false;
  const histBucketAt = (ev) => {
    const r = histCanvas.getBoundingClientRect();
    const g = S.graph; if (!g || !g.hist.length) return null;
    const i = Math.floor(((ev.clientX - r.left) / r.width) * g.hist.length);
    return g.hist[Math.max(0, Math.min(g.hist.length - 1, i))][0];
  };
  histCanvas.onmousedown = (ev) => { brushStart = histBucketAt(ev); brushMoved = false; };
  histCanvas.onmousemove = (ev) => {
    if (brushStart == null) return;
    brushMoved = true;
    const cur = histBucketAt(ev);
    S.brush = brushStart <= cur ? [brushStart, cur] : [cur, brushStart];
    drawHist(); draw();
  };
  window.addEventListener('mouseup', () => {
    if (brushStart != null && !brushMoved && S.brush) { S.brush = null; drawHist(); draw(); }
    brushStart = null;
  });

  // ===== graph interaction ================================================
  const toWorld = (ev) => {
    const r = canvas.getBoundingClientRect();
    return [(ev.clientX - r.left - S.view.x) / S.view.k, (ev.clientY - r.top - S.view.y) / S.view.k];
  };
  const pick = (mx, my) => {
    if (!S.graph) return null;
    for (const n of S.graph.nodes.values()) {
      const dx = mx - n.x, dy = my - n.y;
      if (dx * dx + dy * dy <= (radius(n) + 4) ** 2) return n;
    }
    return null;
  };
  canvas.onmousemove = (ev) => {
    if (!S.graph) return;
    const [mx, my] = toWorld(ev);
    if (S.sim.drag) { S.sim.drag.x = mx; S.sim.drag.y = my; S.sim.hot = Math.max(S.sim.hot, .3); kick(); return; }
    if (S.pan) { S.view.x += ev.movementX; S.view.y += ev.movementY; draw(); return; }
    const n = pick(mx, my);
    if (n !== S.sim.hover) { S.sim.hover = n; kick(); }
    const r = canvas.getBoundingClientRect();
    if (n) {
      tip.style.display = 'block';
      tip.style.left = `${ev.clientX - r.left + 12}px`;
      tip.style.top = `${ev.clientY - r.top + 12}px`;
      tip.textContent = `${n.name} — ${n.deg.toLocaleString()} events`
        + (n.out ? ` · ${n.out.toLocaleString()} outbound` : '');
    } else tip.style.display = 'none';
  };
  canvas.onmousedown = (ev) => {
    const [mx, my] = toWorld(ev);
    const n = pick(mx, my);
    if (n) { S.sim.drag = n; kick(); } else { S.pan = true; }
  };
  window.addEventListener('mouseup', () => { if (S) { if (S.sim) S.sim.drag = null; S.pan = false; } });
  canvas.onwheel = (ev) => {
    ev.preventDefault();
    const r = canvas.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const factor = ev.deltaY < 0 ? 1.1 : 1 / 1.1;
    const k2 = Math.max(0.2, Math.min(4, S.view.k * factor));
    S.view.x = mx - (mx - S.view.x) * (k2 / S.view.k);
    S.view.y = my - (my - S.view.y) * (k2 / S.view.k);
    S.view.k = k2; draw();
  };
  canvas.ondblclick = (ev) => {
    const [mx, my] = toWorld(ev);
    const n = pick(mx, my);
    if (!n) return;
    const sel = S.selections[0];
    if (!sel) return;
    const asSrc = S.graph.edges.some((e) => e.a === n);
    const col = asSrc ? sel.def.src_col : sel.def.dst_col;
    ctx.openFiltered(sel.source_id, [{ column: col, value: n.name }]);
    toast(`Opened rows where ${col} = ${n.name}`);
  };

  new ResizeObserver(() => { kick(); drawHist(); }).observe(stage);

  manage.onclick = () => openEventManager(winnow, renderPanel);
  loadDefs().then(renderPanel);
}

async function loadDefs() {
  try { S.defs = await ctx.api(`${ctx.base}/defs`); }
  catch { S.defs = { shipped: [], saved: [] }; }
}

/* The Events… modal: browse shipped defaults, add/edit/delete saved
   ones. onPanelChange repaints the selection panel behind the modal so a
   just-saved event is immediately tickable. */
function openEventManager(winnow, onPanelChange) {
  const { el, modal } = winnow;
  modal('Movement events', (b) => {
    b.append(el('p', null,
      'Shipped events bind to any table with the right columns. Saved events are yours, '
      + 'kept on this machine across cases. An event maps a source column to a destination '
      + 'column, optionally within conditions (e.g. EventId = 4624).'));
    const list = el('div', 'session-list');
    const render = () => {
      list.replaceChildren();
      for (const d of S.defs.shipped) {
        const row = el('div', 'row-actions session-row');
        row.append(el('span', 'session-name', d.name), el('span', 'count', 'shipped'));
        list.append(row);
      }
      S.defs.saved.forEach((d, i) => {
        const row = el('div', 'row-actions session-row');
        row.append(el('span', 'session-name', d.name), el('span', 'count', `${d.src_col} → ${d.dst_col}`));
        const del = el('button', 'btn ghost', 'Delete');
        del.onclick = async () => { S.defs.saved.splice(i, 1); await saveDefs(winnow); render(); onPanelChange(); };
        row.append(del); list.append(row);
      });
    };
    render();
    b.append(list);
    const add = el('button', 'btn', 'Add a saved event…');
    add.onclick = () => openEventEditor(winnow, async (def) => {
      S.defs.saved.push(def); await saveDefs(winnow); render(); onPanelChange();
    });
    b.append(add);
  }, { wide: true });
}

async function saveDefs(winnow) {
  try { S.defs = await winnow.post(`${winnow.base}/defs`, { saved: S.defs.saved }); }
  catch (e) { winnow.toast('Could not save: ' + e.message, 6000); }
}

function openEventEditor(winnow, onSave) {
  const { el, modal, toast } = winnow;
  const cols = [...new Set(winnow.state.sources.flatMap((s) => s.columns.map((c) => c.name)))].sort();
  const mkColSel = (val) => {
    const s = el('select');
    s.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px 6px;font:inherit';
    s.append(new Option('(column)', ''));
    for (const c of cols) s.append(new Option(c, c));
    if (val) s.value = val;
    return s;
  };
  modal('New movement event', (b) => {
    const name = el('input'); name.className = 'confirm-input'; name.placeholder = 'Name (e.g. WinRM logons)';
    const src = mkColSel(), dst = mkColSel(), label = mkColSel(), time = mkColSel();
    b.append(el('label', null, 'Name'), name);
    b.append(el('label', null, 'Source host column'), src);
    b.append(el('label', null, 'Destination host column'), dst);
    b.append(el('label', null, 'Label column (optional, e.g. user)'), label);
    b.append(el('label', null, 'Time column (optional — enables the timeline)'), time);
    b.append(el('div', 'lm-src-head', 'Conditions (all must hold)'));
    const conds = el('div', 'lm-conditions');
    const rows = [];
    const addCond = (c = {}) => {
      const row = el('div', 'lm-cond-row');
      const col = mkColSel(c.column);
      const op = el('select');
      op.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px 6px;font:inherit';
      for (const o of ['equals', 'not_equals', 'contains', 'not_contains', 'in', 'not_in']) op.append(new Option(o, o));
      if (c.op) op.value = c.op;
      const val = el('input'); val.className = 'confirm-input'; val.value = c.value || '';
      val.placeholder = 'value (comma list for in/not_in)';
      const rm = el('button', 'btn ghost', '✕');
      const entry = { col, op, val };
      rm.onclick = () => { conds.removeChild(row); rows.splice(rows.indexOf(entry), 1); };
      row.append(col, op, val, rm);
      rows.push(entry); conds.append(row);
    };
    const addBtn = el('button', 'btn ghost', '+ Condition');
    addBtn.onclick = () => addCond();
    b.append(conds, addBtn);
    const acts = el('div', 'row-actions');
    const save = el('button', 'btn', 'Save event');
    save.onclick = () => {
      if (!name.value.trim() || !src.value || !dst.value) { toast('Name, source and destination are required'); return; }
      const def = {
        name: name.value.trim(), src_col: src.value, dst_col: dst.value,
        label_col: label.value || null, time_col: time.value || null,
        requires: [src.value, dst.value, ...(time.value ? [time.value] : [])],
        conditions: rows.map((r) => ({ column: r.col.value, op: r.op.value, value: r.val.value })).filter((c) => c.column),
        color: '#4ac7c7',
      };
      document.getElementById('modal').hidden = true;
      onSave(def);
    };
    acts.append(save); b.append(acts);
  });
}

export function onShow() {
  // Sources/timeframe may have changed while another tab was active.
  if (S && ctx) {
    loadDefs().then(() => {
      S.selections = S.selections.filter((s) => ctx.state.sources.some((src) => src.id === s.source_id));
    });
  }
}
