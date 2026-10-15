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
        bucket: 'day', brush: null, sim: null, view: { x: 0, y: 0, k: 1 }, pan: false,
        panelOpen: S && S.panelOpen !== undefined ? S.panelOpen : true };

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

  // ---- event-selection panel (collapsible) ------------------------------
  const panelHead = el('div', 'lm-panel-head');
  const caret = el('button', 'lm-caret');
  caret.title = 'Show/hide the event list';
  const summary = el('span', 'lm-panel-summary');
  const newBtn = el('button', 'btn ghost', '+ New event type');
  newBtn.title = 'Define a movement event mapped to your own columns';
  newBtn.onclick = () => openEventBuilder(winnow, { onSave: addEvent });
  panelHead.append(caret, el('span', 'lm-panel-title', 'Movement events'), summary, newBtn);
  root.append(panelHead);
  const panel = el('div', 'lm-events');
  root.append(panel);

  function paintPanelHead() {
    caret.textContent = S.panelOpen ? '▾' : '▸';
    panel.hidden = !S.panelOpen;
    const n = S.selections.length;
    summary.textContent = n
      ? `${n} event${n === 1 ? '' : 's'} selected`
      : 'none selected';
  }
  caret.onclick = () => { S.panelOpen = !S.panelOpen; paintPanelHead(); };

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
    return [...S.defs.shipped, ...S.defs.saved].filter((d) => bindsTo(d, src));
  }

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
    paintPanelHead();
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

  // A definition saved, edited or deleted from a dialog repaints the
  // panel behind it, with the ticks carried over (syncSelections).
  repaint = () => { syncSelections(); recolor(); };
  manage.onclick = () => openEventManager(winnow);
  paintPanelHead();
  loadDefs().then(() => { renderPanel(); paintPanelHead(); });
}

let repaint = () => {};
const selKey = (sourceId, name) => `${sourceId}::${name}`;

/* Whether a definition is offered on a table. Tied to a header set, it
   binds only on a table that IS that artifact (has every column the set
   defines), the way a filter default binds; a set that isn't known
   (older defs) falls back to the explicit column list. Either way the
   table must also carry every column the event itself reads: a
   condition on a column the table lacks used to bind anyway and fail
   at Build with "No column". */
function bindsTo(d, src) {
  const cols = new Set(src.columns.map((c) => c.name));
  const hs = d.header_set && S.defs.header_sets && S.defs.header_sets[d.header_set];
  if (hs && !hs.every((c) => cols.has(c))) return false;
  return [...(d.requires || []), ...requiresOf(d)].every((c) => cols.has(c));
}

// Every column an event reads, in the order the builder shows them.
function requiresOf(d) {
  return [...new Set([d.src_col, d.dst_col, d.label_col, d.time_col,
    ...(d.conditions || []).map((c) => c.column)].filter(Boolean))];
}

/* Ticks follow the definitions they name: an edited event keeps its
   ticks where it still binds, a deleted one loses them. Before this a
   deleted event stayed ticked, and Build sent it anyway. */
function syncSelections() {
  const byName = new Map([...S.defs.shipped, ...S.defs.saved].map((d) => [d.name, d]));
  S.selections = S.selections.flatMap((s) => {
    const d = byName.get(s.def.name);
    const src = ctx.state.sources.find((x) => x.id === s.source_id);
    return d && src && bindsTo(d, src) ? [{ ...s, def: d }] : [];
  });
}

// A freshly defined event is usually the one you want next: tick it
// wherever it binds, so Build is one click away.
async function addEvent(def) {
  S.defs.saved.push(def);
  await saveDefs(ctx);
  for (const src of ctx.state.sources.filter((x) => !x.error)) {
    if (bindsTo(def, src) && !S.selections.some((sl) => sl.key === selKey(src.id, def.name))) {
      S.selections.push({ key: selKey(src.id, def.name), source_id: src.id, def, color: null });
    }
  }
  repaint();
}

async function replaceEvent(oldName, def) {
  const i = S.defs.saved.findIndex((d) => d.name === oldName);
  if (i < 0) return addEvent(def);
  S.defs.saved[i] = def;
  // A rename carries its ticks across to the new name.
  for (const s of S.selections) {
    if (s.def.name === oldName) { s.def = def; s.key = selKey(s.source_id, def.name); }
  }
  await saveDefs(ctx);
  repaint();
}

async function loadDefs() {
  try { S.defs = await ctx.api(`${ctx.base}/defs`); }
  catch { S.defs = { shipped: [], saved: [] }; }
}

async function saveDefs(winnow) {
  try { S.defs = await winnow.post(`${winnow.base}/defs`, { saved: S.defs.saved }); }
  catch (e) { winnow.toast('Could not save: ' + e.message, 6000); }
}

/* The Events… modal: every definition with the hop it draws and where it
   binds. Shipped ones can be copied into the builder as a starting
   point; saved ones edited or deleted. */
function openEventManager(winnow) {
  const { el, modal, confirmDialog } = winnow;
  const reopen = () => openEventManager(winnow);
  modal('Movement events', (b) => {
    b.append(el('p', 'note-status',
      'Shipped events come with the plugin. Yours are kept on this machine and offered in every case. '
      + 'Copy a shipped event to start a new one from it.'));
    const list = el('div', 'lm-mgr');
    const row = (d, kind) => {
      const r = el('div', 'lm-mgr-row');
      const text = el('div', 'lm-mgr-text');
      text.append(el('span', 'lm-mgr-name', d.name),
                  el('span', 'lm-mgr-sub', `${d.src_col} → ${d.dst_col} · ${bindingText(d)}`));
      r.append(text);
      const acts = el('div', 'lm-mgr-acts');
      if (kind === 'shipped') {
        const copy = el('button', 'btn ghost', 'Copy to edit');
        copy.onclick = () => openEventBuilder(winnow, { template: d, onSave: addEvent, onDone: reopen });
        acts.append(copy);
      } else {
        const edit = el('button', 'btn ghost', 'Edit');
        edit.onclick = () => openEventBuilder(winnow, {
          edit: d, onSave: (def) => replaceEvent(d.name, def), onDone: reopen });
        const del = el('button', 'btn ghost', 'Delete');
        del.onclick = async () => {
          if (!(await confirmDialog(`Delete the movement event "${d.name}"?`, { okLabel: 'Delete', danger: true }))) return;
          S.defs.saved = S.defs.saved.filter((x) => x !== d);
          await saveDefs(winnow);
          repaint();
          reopen();
        };
        acts.append(edit, del);
      }
      r.append(acts);
      return r;
    };
    if (S.defs.saved.length) {
      list.append(el('div', 'lm-mgr-head', 'Yours'));
      for (const d of S.defs.saved) list.append(row(d, 'saved'));
    }
    list.append(el('div', 'lm-mgr-head', 'Shipped'));
    for (const d of S.defs.shipped) list.append(row(d, 'shipped'));
    b.append(list);
    const add = el('button', 'btn', '+ New event type');
    add.onclick = () => openEventBuilder(winnow, { onSave: addEvent, onDone: reopen });
    const acts = el('div', 'row-actions');
    acts.append(add);
    b.append(acts);
  }, { wide: true });
}

function bindingText(d) {
  return d.header_set ? `${d.header_set} tables` : 'any table with its columns';
}

/* ============================================================ event builder

   One dialog for a new event, a copy of a shipped one, or an edit of one
   of yours. It has the merge builder's shape: the definition on the
   left, read against one real table in the case, and on the right what
   that definition finds there (rows matched, links, hosts, the busiest
   links), refreshed as it changes. A wrong column or a condition value
   spelled the way the export doesn't spell it shows up here, before the
   event is saved, instead of as an empty graph afterwards. */

const OP_LABELS = [
  ['equals', 'is'], ['not_equals', 'is not'], ['in', 'is one of'],
  ['not_in', 'is none of'], ['contains', 'contains'], ['not_contains', 'does not contain'],
];
const LIST_OPS = new Set(['in', 'not_in']);

/* Column guesses, per role and per table: the first pattern that matches
   any column wins, so an exact name (UserName) beats a loose one (the
   SID in UserId, which the old union-of-every-table guess picked). */
const GUESS = {
  src: [/^(remote|source|src|client|calling)[ _-]?(host|hostname|ip|ip ?address|addr(ess)?|name|workstation|computer)?$/i,
        /workstation|ip ?address|remote|source|src|client|caller/i],
  dst: [/^(dest(ination)?|dst|target)[ _-]?(host|hostname|ip|ip ?address|addr(ess)?|name|computer|server)?$/i,
        /^computer(name)?$/i, /dest|dst|target|server|computer|host/i],
  label: [/^(target)?user[ _-]?name$/i, /^(user|account|subject|principal)$/i, /user|account|principal|subject/i],
  time: [/^(time ?created|timestamp|time ?generated|event ?time|date ?time)$/i, /time|date|created|when|utc/i],
};
const ID_LIKE = /(Id|ID|Sid|SID|[_\s-]id)$/;

function guessColumn(src, role, avoid) {
  let cols = src.columns.filter((c) => !avoid.includes(c.name));
  if (role === 'label') cols = cols.filter((c) => !ID_LIKE.test(c.name));
  const tiers = role === 'time' ? [cols.filter((c) => c.type === 'datetime'), cols] : [cols];
  for (const re of GUESS[role]) {
    for (const tier of tiers) {
      const hit = tier.find((c) => re.test(c.name));
      if (hit) return hit.name;
    }
  }
  if (role === 'time') { const dt = cols.find((c) => c.type === 'datetime'); if (dt) return dt.name; }
  return '';
}

function headerSetsOf(src) {
  const cols = new Set(src.columns.map((c) => c.name));
  return Object.entries(S.defs.header_sets || {})
    .filter(([, hs]) => hs.every((c) => cols.has(c)))
    .sort((a, b) => b[1].length - a[1].length)
    .map(([name]) => name);
}

function openEventBuilder(winnow, { edit = null, template = null, onSave, onDone = null } = {}) {
  const { el, modal, post, closeModal } = winnow;
  const sources = winnow.state.sources.filter((s) => !s.error);
  const taken = new Map([...S.defs.shipped.map((d) => [d.name.toLowerCase(), 'a shipped event']),
    ...S.defs.saved.filter((d) => d !== edit).map((d) => [d.name.toLowerCase(), 'one of your events'])]);
  const st = {
    sourceId: null, binding: '', bindingTouched: false,
    name: '', nameTouched: false, base: null, description: '',
    src_col: '', dst_col: '', label_col: '', time_col: '', strip_prefix: false, conditions: [],
    // Roles the definition or the analyst chose, as opposed to guessed:
    // a table switch re-guesses only the rest, so previewing an edited
    // event against another table never quietly rewrites it.
    picked: new Set(),
  };
  const srcOf = () => sources.find((s) => s.id === st.sourceId) || null;
  const hasCol = (name) => !!srcOf() && srcOf().columns.some((c) => c.name === name);

  // Copy a definition into the builder and read it against a table it
  // binds to, when the case has one.
  function load(def) {
    for (const role of ['src', 'dst', 'label', 'time']) {
      st[`${role}_col`] = def[`${role}_col`] || '';
      if (st[`${role}_col`]) st.picked.add(role);
    }
    st.strip_prefix = !!def.strip_prefix;
    st.conditions = (def.conditions || []).map((c) => ({ column: c.column, op: c.op, value: c.value ?? '' }));
    st.description = def.description || '';
    st.binding = def.header_set || '';
    st.bindingTouched = true;
    const home = sources.find((s) => bindsTo(def, s));
    if (home) st.sourceId = home.id;
  }
  // Guess every role nobody chose from the table's own column names.
  function guessMissing() {
    const src = srcOf();
    if (!src) return;
    for (const role of ['src', 'dst', 'label', 'time']) {
      if (st.picked.has(role)) continue;
      const avoid = ['src', 'dst'].filter((r) => r !== role && st.picked.has(r)).map((r) => st[`${r}_col`]);
      if (role === 'dst' && st.src_col) avoid.push(st.src_col);
      st[`${role}_col`] = guessColumn(src, role, avoid);
    }
    if (!st.bindingTouched) st.binding = headerSetsOf(src)[0] || '';
  }

  if (edit) { load(edit); st.name = edit.name; st.nameTouched = true; }
  else if (template) { load(template); st.base = template.name; }
  if (st.sourceId == null) {
    // The open table when it has something that reads as a hop, else
    // the first table that does.
    const hoppy = sources.filter((s) => guessColumn(s, 'src', []) && guessColumn(s, 'dst', [guessColumn(s, 'src', [])]));
    const cur = (id) => id === winnow.state.sourceId;
    st.sourceId = (hoppy.find((s) => cur(s.id)) || hoppy[0] || sources.find((s) => cur(s.id)) || sources[0] || {}).id ?? null;
  }
  if (!edit && !template) guessMissing();

  // A name that follows the definition until the analyst types one —
  // "EventId 5985: RemoteHost → Computer" — never one that's taken.
  function suggestedName() {
    let base = st.base ? `${st.base} (copy)` : '';
    if (!base) {
      const conds = st.conditions.filter((c) => c.column && String(c.value).trim()
        && ['equals', 'in'].includes(c.op)).map((c) => `${c.column} ${String(c.value).trim()}`);
      const hop = st.src_col && st.dst_col ? `${st.src_col} → ${st.dst_col}` : 'New movement event';
      base = conds.length ? `${conds.join(', ')}: ${hop}` : hop;
    }
    let name = base;
    for (let n = 2; taken.has(name.toLowerCase()); n++) name = `${base} (${n})`;
    return name;
  }
  const currentName = () => (st.nameTouched ? st.name : suggestedName()).trim();

  // A condition counts once it has a column and a value. A row just
  // added has neither, and previewing "EventId is (blank)" would only
  // ever report that nothing matches.
  const hasValue = (c) => {
    const v = String(c.value ?? '');
    return (LIST_OPS.has(c.op) ? v.split(',') : [v]).some((x) => x.trim());
  };
  const usableConditions = () => st.conditions.filter((c) => c.column && hasValue(c));
  const waitingConditions = () => st.conditions.filter((c) => c.column && !hasValue(c));
  function problem() {
    if (!st.src_col || !st.dst_col) return 'Pick a source and a destination column.';
    if (st.src_col === st.dst_col) return 'Source and destination need to be different columns.';
    const half = waitingConditions()[0];
    if (half) return `The ${half.column} condition needs a value, or remove it.`;
    if (st.conditions.some((c) => !c.column && hasValue(c))) return 'Pick a column for every condition, or remove it.';
    const name = currentName();
    if (!name) return 'Give the event a name.';
    if (taken.has(name.toLowerCase())) return `"${name}" is already the name of ${taken.get(name.toLowerCase())}.`;
    return '';
  }

  let tableSel, segBox, segNote, roleSels = {}, stripCb, condBox, nameInput, descInput, saveBtn, hint;
  let statsBox, notesBox, linksBox, previewHead;
  // Declared before modal() runs the builder, which already uses them.
  let timer = 0, seq = 0;
  const valueCache = new Map();

  modal(edit ? `Edit movement event — ${edit.name}` : 'New movement event', (b) => {
    if (!sources.length) {
      b.append(el('p', null, 'Import a table first. The builder reads its columns from one, and shows what the event finds there.'));
      return;
    }
    const root = el('div', 'lm-eb');
    const panes = el('div', 'lm-eb-panes');
    const left = el('div', 'lm-eb-pane');

    // ---- start from (a new event only)
    if (!edit) {
      const startSel = el('select');
      startSel.setAttribute('aria-label', 'Start from');
      startSel.append(new Option('A blank event', ''));
      for (const [label, defs] of [['Shipped', S.defs.shipped], ['Yours', S.defs.saved]]) {
        if (!defs.length) continue;
        const g = document.createElement('optgroup');
        g.label = label;
        defs.forEach((d, i) => g.append(new Option(d.name, `${label}:${i}`)));
        startSel.append(g);
      }
      if (template) {
        const i = S.defs.shipped.indexOf(template), j = S.defs.saved.indexOf(template);
        startSel.value = i >= 0 ? `Shipped:${i}` : (j >= 0 ? `Yours:${j}` : '');
      }
      startSel.onchange = () => {
        const [kind, i] = startSel.value.split(':');
        const def = kind ? (kind === 'Shipped' ? S.defs.shipped : S.defs.saved)[+i] : null;
        Object.assign(st, { src_col: '', dst_col: '', label_col: '', time_col: '', strip_prefix: false,
          conditions: [], description: '', base: null, bindingTouched: false, picked: new Set() });
        if (def) { load(def); st.base = def.name; tableSel.value = String(st.sourceId); }
        guessMissing();
        refresh();
      };
      left.append(section('Start from', startSel));
    }

    // ---- which table the columns come from, and where the event is offered
    tableSel = el('select');
    tableSel.setAttribute('aria-label', 'Read columns from');
    for (const s of sources) tableSel.append(new Option(`${s.name} · ${s.row_count.toLocaleString()} rows`, String(s.id)));
    tableSel.value = String(st.sourceId);
    tableSel.onchange = () => { st.sourceId = +tableSel.value; guessMissing(); refresh(); };
    segBox = el('div', 'lm-eb-seg');
    segBox.setAttribute('role', 'group');
    segBox.setAttribute('aria-label', 'Offer it on');
    segNote = el('div', 'lm-eb-sub');
    const offer = el('div', 'lm-eb-offer');
    offer.append(el('span', 'lm-eb-label', 'Offer it on'), segBox);
    left.append(section('Read columns from', tableSel, offer, segNote));

    // ---- the hop
    const hop = el('div', 'lm-eb-pair');
    roleSels.src = colSelect('src_col', 'Pick a column');
    roleSels.dst = colSelect('dst_col', 'Pick a column');
    hop.append(field('Source host', roleSels.src), el('span', 'lm-eb-arrow', '→'), field('Destination host', roleSels.dst));
    const strip = el('label', 'lm-eb-check');
    stripCb = el('input');
    stripCb.type = 'checkbox';
    stripCb.onchange = () => { st.strip_prefix = stripCb.checked; changed(); };
    strip.title = 'EVTX payload fields carry their field name in the value: "TargetServerName: FILESRV" becomes FILESRV.';
    strip.append(stripCb, el('span', null, 'Drop a leading "Name: " from both host values'));
    left.append(section('The hop', hop, strip));

    // ---- optional columns
    const extra = el('div', 'lm-eb-pair two');
    roleSels.label = colSelect('label_col', '(none)');
    roleSels.time = colSelect('time_col', '(none)');
    extra.append(field('User column', roleSels.label, 'Counted per link: how many users made each hop'),
                 field('Time column', roleSels.time, 'Puts the hops on the timeline under the graph'));
    left.append(section('Also read (optional)', extra));

    // ---- conditions
    condBox = el('div', 'lm-eb-conds');
    const addCond = el('button', 'btn ghost lm-eb-small', '+ Add a condition');
    addCond.type = 'button';
    addCond.onclick = () => { st.conditions.push({ column: '', op: 'equals', value: '' }); renderConds(); changed(); };
    left.append(section('Only rows where', condBox, addCond));

    // ---- preview
    const right = el('div', 'lm-eb-pane lm-eb-result');
    previewHead = el('div', 'lm-eb-head');
    statsBox = el('div', 'lm-eb-stats');
    notesBox = el('ul', 'lm-eb-notes');
    linksBox = el('div', 'lm-eb-links');
    right.append(previewHead, statsBox, notesBox, el('div', 'lm-eb-head', 'Busiest links'), linksBox);
    panes.append(left, right);

    // ---- name and save
    const foot = el('div', 'lm-eb-foot');
    nameInput = el('input');
    nameInput.type = 'text';
    nameInput.autocomplete = 'off';
    nameInput.id = 'lmEbName';
    if (st.nameTouched) nameInput.value = st.name;
    nameInput.oninput = () => { st.nameTouched = nameInput.value.trim() !== ''; st.name = nameInput.value; renderFoot(); };
    nameInput.onkeydown = (e) => { if (e.key === 'Enter' && !saveBtn.disabled) saveBtn.click(); };
    descInput = el('input');
    descInput.type = 'text';
    descInput.autocomplete = 'off';
    descInput.id = 'lmEbDesc';
    descInput.placeholder = 'What this hop means, shown beside it in the list';
    descInput.value = st.description;
    descInput.oninput = () => { st.description = descInput.value; };
    saveBtn = el('button', 'btn lm-eb-save', edit ? 'Save changes' : 'Save event');
    saveBtn.type = 'button';
    saveBtn.onclick = save;
    hint = el('div', 'lm-eb-hint');
    foot.append(footField('Name', nameInput), footField('Description (optional)', descInput), saveBtn, hint);

    root.append(panes, foot);
    b.append(root);
    refresh();
  }, { wide: 'x', focus: '#lmEbName' });

  function section(title, ...kids) {
    const s = el('div', 'lm-eb-sec');
    s.append(el('div', 'lm-eb-head', title), ...kids);
    return s;
  }
  function field(label, control, sub) {
    const f = el('label', 'lm-eb-field');
    f.append(el('span', 'lm-eb-label', label), control);
    if (sub) f.append(el('small', null, sub));
    return f;
  }
  function footField(label, input) {
    const f = el('div', 'lm-eb-field');
    const l = el('label', 'lm-eb-head', label);
    l.htmlFor = input.id;
    f.append(l, input);
    return f;
  }
  function colSelect(key, emptyLabel) {
    const s = el('select');
    s.dataset.role = key;
    s.dataset.empty = emptyLabel;
    s.onchange = () => { st[key] = s.value; st.picked.add(key.replace('_col', '')); fillColumns(); changed(); };
    return s;
  }
  // Options are the chosen table's columns, in the table's own order. A
  // value the table doesn't have (an event made for another case's
  // tables) stays selected and says so, rather than silently becoming
  // the first column.
  function fillSelect(s, value, emptyLabel) {
    const src = srcOf();
    s.replaceChildren(new Option(emptyLabel, ''));
    for (const c of src.columns) s.append(new Option(c.name, c.name));
    if (value && !hasCol(value)) s.append(new Option(`${value} (not in this table)`, value));
    s.value = value || '';
    s.classList.toggle('missing', !!value && !hasCol(value));
  }
  function fillColumns() {
    for (const s of Object.values(roleSels)) fillSelect(s, st[s.dataset.role], s.dataset.empty);
    stripCb.checked = st.strip_prefix;
  }
  function paintBinding() {
    const src = srcOf();
    const sets = headerSetsOf(src);
    if (st.binding && !sets.includes(st.binding)) sets.push(st.binding);
    segBox.replaceChildren();
    const opts = [...sets.map((n) => [n, `${n} tables`]), ['', 'Any table with these columns']];
    for (const [value, label] of opts) {
      const btn = el('button', null, label);
      btn.type = 'button';
      btn.setAttribute('aria-pressed', String(st.binding === value));
      btn.onclick = () => { st.binding = value; st.bindingTouched = true; paintBinding(); changed(); };
      segBox.append(btn);
    }
    const cols = requiresOf(st);
    const here = sources.filter((s) => bindsTo(draftDef(), s));
    const names = here.slice(0, 3).map((s) => s.name).join(', ') + (here.length > 3 ? ` and ${here.length - 3} more` : '');
    segNote.textContent = (st.binding
      ? `Offered on every table that has the ${st.binding} columns`
      : `Offered on every table that has ${cols.length ? cols.join(', ') : 'the columns you pick'}`)
      + (here.length ? `. In this case: ${names}.` : '. No table in this case has them all.');
  }

  function renderConds() {
    condBox.replaceChildren();
    if (!st.conditions.length) {
      condBox.append(el('p', 'lm-eb-empty', 'Every row with both hosts counts. Add a condition to narrow it, for example EventId is 4624.'));
      return;
    }
    st.conditions.forEach((c) => {
      const row = el('div', 'lm-eb-cond');
      const col = el('select');
      col.setAttribute('aria-label', 'Condition column');
      fillSelect(col, c.column, 'Pick a column');
      const op = el('select');
      op.setAttribute('aria-label', 'Condition operator');
      for (const [o, label] of OP_LABELS) op.append(new Option(label, o));
      op.value = c.op || 'equals';
      const val = el('input');
      val.type = 'text';
      val.setAttribute('aria-label', 'Condition value');
      val.value = c.value ?? '';
      val.placeholder = LIST_OPS.has(op.value) ? 'value, value, …' : 'value';
      const rm = el('button', 'btn ghost lm-eb-x', '✕');
      rm.type = 'button';
      rm.title = 'Remove this condition';
      const chips = el('div', 'lm-eb-vals');
      col.onchange = () => { c.column = col.value; col.classList.toggle('missing', false); loadValues(c, chips, val); changed(); };
      op.onchange = () => {
        c.op = op.value;
        val.placeholder = LIST_OPS.has(op.value) ? 'value, value, …' : 'value';
        paintChips(c, chips, val);
        changed();
      };
      val.oninput = () => { c.value = val.value; paintChips(c, chips, val); changed(); };
      rm.onclick = () => { st.conditions.splice(st.conditions.indexOf(c), 1); renderConds(); changed(); };
      row.append(col, op, val, rm, chips);
      condBox.append(row);
      loadValues(c, chips, val);
    });
  }

  // The commonest values of a condition's column, as chips: a click sets
  // the value, or toggles it in and out of an "is one of" list.
  function loadValues(c, chips, input) {
    chips.replaceChildren();
    if (!c.column || !hasCol(c.column)) return;
    const key = `${st.sourceId}::${c.column}`;
    if (!valueCache.has(key)) {
      valueCache.set(key, post(`${winnow.base}/values`, { source_id: st.sourceId, column: c.column })
        .then((r) => r.values).catch(() => []));
    }
    valueCache.get(key).then((vals) => { chips._vals = vals; paintChips(c, chips, input); });
  }
  function paintChips(c, chips, input) {
    const vals = chips._vals || [];
    chips.replaceChildren();
    const list = LIST_OPS.has(c.op);
    const picked = list ? String(c.value).split(',').map((v) => v.trim()).filter(Boolean) : [String(c.value).trim()];
    for (const { value, n } of vals.slice(0, 8)) {
      const chip = el('button', 'lm-eb-chip');
      chip.type = 'button';
      chip.title = `${value} — ${n.toLocaleString()} rows`;
      chip.setAttribute('aria-pressed', String(picked.includes(value)));
      chip.append(el('b', null, value), document.createTextNode(` ${n.toLocaleString()}`));
      chip.onclick = () => {
        if (list) {
          const next = picked.includes(value) ? picked.filter((v) => v !== value) : [...picked, value];
          c.value = next.join(', ');
        } else c.value = value;
        input.value = c.value;
        paintChips(c, chips, input);
        changed();
      };
      chips.append(chip);
    }
  }

  // The definition as it would be saved right now.
  function draftDef() {
    const conditions = usableConditions().map((c) => ({ column: c.column, op: c.op, value: String(c.value) }));
    const def = {
      name: currentName(), src_col: st.src_col, dst_col: st.dst_col,
      label_col: st.label_col || null, time_col: st.time_col || null,
      strip_prefix: st.strip_prefix,
      header_set: st.binding || null,
      conditions,
      color: (edit || template || {}).color || '#4ac7c7',
    };
    if (st.description.trim()) def.description = st.description.trim();
    def.requires = requiresOf(def);
    return def;
  }

  function changed() {
    paintBinding();
    renderFoot();
    clearTimeout(timer);
    timer = setTimeout(runPreview, 180);
  }
  function refresh() {
    tableSel.value = String(st.sourceId);
    fillColumns();
    renderConds();
    descInput.value = st.description;
    changed();
  }
  function renderFoot() {
    if (!st.nameTouched) nameInput.value = suggestedName();
    const p = problem();
    hint.textContent = p;
    saveBtn.disabled = !!p;
  }

  async function runPreview() {
    const my = ++seq;
    const src = srcOf();
    previewHead.textContent = `What it finds in ${src.name}`;
    const missing = requiresOf(st).filter((c) => !hasCol(c));
    if (!st.src_col || !st.dst_col) return paintPreview(null, ['Pick a source and a destination column to see the links this event finds.']);
    if (st.src_col === st.dst_col) return paintPreview(null, ['Source and destination are the same column, so every row would be a host reaching itself.'], 'warn');
    if (missing.length) return paintPreview(null, [`This table has no ${missing.join(', ')} column. Pick another table to preview against.`], 'warn');
    statsBox.classList.add('busy');
    try {
      const r = await post(`${winnow.base}/preview`, { selection: { source_id: st.sourceId, ...draftDef() } });
      if (my === seq) paintPreview(r);
    } catch (e) {
      if (my === seq) paintPreview(null, [e.message], 'warn');
    }
  }

  function paintPreview(r, msgs = [], kind = 'info') {
    statsBox.classList.remove('busy');
    statsBox.replaceChildren();
    const stat = (v, label) => {
      const s = el('div', 'lm-eb-stat');
      s.append(el('b', null, v == null ? '—' : v.toLocaleString()), el('span', null, label));
      statsBox.append(s);
    };
    stat(r && r.matched, 'rows match');
    stat(r && r.links, r && r.links_capped ? 'links (or more)' : 'links');
    stat(r && r.hosts, 'hosts');
    notesBox.replaceChildren();
    const note = (text, k = 'info') => { const li = el('li', k, text); notesBox.append(li); };
    for (const m of msgs) note(m, kind);
    linksBox.replaceChildren();
    if (!r) { linksBox.append(el('p', 'lm-eb-empty', 'Nothing to show yet.')); return; }
    for (const c of waitingConditions()) note(`The ${c.column} condition has no value yet, so this leaves it out.`);

    if (!r.matched) note(usableConditions().length
      ? 'No rows in this table match the conditions. The value chips under each condition show how the column is actually spelled.'
      : 'This table is empty.', 'warn');
    else if (r.matched > r.hops) {
      note(`${(r.matched - r.hops).toLocaleString()} of the ${r.matched.toLocaleString()} matching rows draw nothing: `
        + 'the source or destination is empty, "-", loopback, or the same host on both ends.');
    }
    if (st.time_col) {
      if (r.first) note(`Hops from ${r.first} to ${r.last}.`, 'ok');
      else if (r.hops) note(`${st.time_col} holds nothing that reads as a time in these rows, so the timeline won't show this event.`, 'warn');
    } else if (r.hops) {
      note('No time column, so the timeline under the graph will not show this event.');
    }

    if (!r.top.length) { linksBox.append(el('p', 'lm-eb-empty', 'No links.')); return; }
    const t = el('table', 'lm-eb-table');
    const head = el('tr');
    for (const h of ['Source', '', 'Destination', 'Rows', ...(st.label_col ? [st.label_col] : [])]) head.append(el('th', null, h));
    const thead = el('thead');
    thead.append(head);
    const body = el('tbody');
    const max = r.top[0].n;
    for (const e of r.top) {
      const tr = el('tr');
      const bar = el('td', 'num');
      const fill = el('span', 'lm-eb-bar');
      fill.style.width = `${Math.max(4, (e.n / max) * 100)}%`;
      bar.append(fill, el('span', null, e.n.toLocaleString()));
      tr.append(el('td', 'host', e.src), el('td', 'arrow', '→'), el('td', 'host', e.dst), bar);
      if (st.label_col) tr.append(el('td', 'num', e.labels == null ? '' : e.labels.toLocaleString()));
      for (const td of tr.querySelectorAll('.host')) td.title = td.textContent;
      body.append(tr);
    }
    t.append(thead, body);
    linksBox.append(t);
    if (r.links > r.top.length) {
      linksBox.append(el('p', 'lm-eb-empty', `and ${(r.links - r.top.length).toLocaleString()} more link${r.links - r.top.length === 1 ? '' : 's'}`));
    }
  }

  async function save() {
    if (problem()) return;
    const def = draftDef();
    saveBtn.disabled = true;
    closeModal();
    await onSave(def);
    if (onDone) onDone();
  }
}

export function onShow() {
  // Sources/timeframe may have changed while another tab was active.
  if (S && ctx) loadDefs().then(() => repaint());
}
