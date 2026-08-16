/* Lateral-movement graph tab — the reference example for a plugin-shipped
   UI (see plugin_api.py's register_tab contract). Mounted once by
   showPluginTab in app.js; `winnow` is the stable context object. All
   colors come from Winnow's own CSS tokens (read at draw time, so theme
   switches just repaint correctly). */

let state = null;   // survives tab switches — the mount lives until case switch / plugin reload
let repaint = null; // set by mount; used by onShow

export default function mount(container, winnow) {
  const { el, post, toast } = winnow;
  state = { nodes: new Map(), edges: [], hot: 0, hover: null, drag: null, raf: 0 };

  // ---- toolbar -----------------------------------------------------------
  const bar = el('div');
  bar.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px;border-bottom:1px solid var(--line-2);flex:0 0 auto';
  const mkSel = (title) => {
    const s = el('select');
    s.title = title;
    s.style.cssText = 'background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:5px 8px;font:inherit;max-width:180px';
    return s;
  };
  const srcSel = mkSel('Table'), fromSel = mkSel('Source column (who initiated)'),
    toSel = mkSel('Destination column (where they went)'), labelSel = mkSel('Optional label column (e.g. user) — counts distinct values per edge');
  const build = el('button', 'btn', 'Build graph');
  const status = el('span', 'note-status', 'Pick a table and its source/destination columns.');
  bar.append(srcSel, el('span', 'note-status', '→'), toSel, labelSel, build, status);
  bar.insertBefore(fromSel, bar.children[1]);
  container.append(bar);

  const wrap = el('div');
  wrap.style.cssText = 'position:relative;flex:1 1 auto;min-height:0';
  const canvas = el('canvas');
  canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%';
  const tip = el('div');
  tip.style.cssText = 'position:absolute;pointer-events:none;background:var(--panel-3);color:var(--text);border:1px solid var(--line-2);padding:3px 7px;font:12px var(--mono);display:none;z-index:2';
  wrap.append(canvas, tip);
  container.append(wrap);

  // ---- column pickers ----------------------------------------------------
  const guess = (cols, res) => (cols.find((c) => res.test(c)) || '');
  function fillColumns() {
    const src = winnow.state.sources.find((s) => String(s.id) === srcSel.value);
    const cols = src ? src.columns.map((c) => c.name) : [];
    for (const [sel, extra] of [[fromSel, false], [toSel, false], [labelSel, true]]) {
      sel.replaceChildren();
      if (extra) sel.append(new Option('(no label)', ''));
      for (const c of cols) sel.append(new Option(c, c));
    }
    fromSel.value = guess(cols, /source|src|ip.?addr|workstation|client|from/i) || cols[0] || '';
    toSel.value = guess(cols, /dest|dst|target|computer|host|to\b/i) || cols[1] || '';
    labelSel.value = guess(cols, /user|account|subject|logon/i) || '';
  }
  function fillSources() {
    const keep = srcSel.value;
    srcSel.replaceChildren();
    for (const s of winnow.state.sources.filter((x) => !x.is_merge && !x.error)) {
      srcSel.append(new Option(`${s.name} (${s.row_count.toLocaleString()})`, String(s.id)));
    }
    if (keep && [...srcSel.options].some((o) => o.value === keep)) {
      srcSel.value = keep; // an onShow refresh shouldn't discard the analyst's picks
    } else {
      fillColumns();
    }
  }
  srcSel.onchange = fillColumns;
  fillSources();

  // ---- graph build -------------------------------------------------------
  build.onclick = async () => {
    if (!srcSel.value) { toast('Import a table first'); return; }
    build.disabled = true;
    status.textContent = 'Building…';
    try {
      const r = await post(`${winnow.base}/edges`, {
        source_id: Number(srcSel.value), src_col: fromSel.value, dst_col: toSel.value,
        label_col: labelSel.value || null,
      });
      loadGraph(r.edges);
      status.textContent = `${state.nodes.size} hosts, ${r.edges.length} edges`
        + (r.truncated ? ' (top edges only — narrow the data for the rest)' : '');
    } catch (e) {
      status.textContent = '';
      toast('Graph failed: ' + e.message, 6000);
    }
    build.disabled = false;
  };

  function loadGraph(edges) {
    const w = canvas.clientWidth || 800, h = canvas.clientHeight || 500;
    state.nodes = new Map();
    const node = (name) => {
      let n = state.nodes.get(name);
      if (!n) {
        n = { name, x: w / 2 + (Math.random() - 0.5) * w * 0.6, y: h / 2 + (Math.random() - 0.5) * h * 0.6, vx: 0, vy: 0, deg: 0, out: 0 };
        state.nodes.set(name, n);
      }
      return n;
    };
    state.edges = edges.map((e) => {
      const a = node(e.src), b2 = node(e.dst);
      a.deg += e.n; a.out += e.n; b2.deg += e.n;
      return { a, b: b2, n: e.n, labels: e.labels };
    });
    state.hot = 1;
    kick();
  }

  // ---- simulation + draw -------------------------------------------------
  function kick() { if (!state.raf) state.raf = requestAnimationFrame(tick); }
  function tick() {
    state.raf = 0;
    step();
    draw();
    if (state.hot > 0.02 || state.drag) kick();
  }
  function step() {
    const nodes = [...state.nodes.values()];
    const w = canvas.clientWidth, h = canvas.clientHeight;
    // Plain O(n²) repulsion — fine at the few hundred hosts a triage graph has.
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        const d2 = Math.max(dx * dx + dy * dy, 25);
        const f = (1400 * state.hot) / d2;
        const d = Math.sqrt(d2);
        dx /= d; dy /= d;
        a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
      }
    }
    for (const e of state.edges) {
      const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d - 120) * 0.01 * state.hot;
      e.a.vx += (dx / d) * f; e.a.vy += (dy / d) * f;
      e.b.vx -= (dx / d) * f; e.b.vy -= (dy / d) * f;
    }
    for (const n of nodes) {
      n.vx += (w / 2 - n.x) * 0.002 * state.hot;
      n.vy += (h / 2 - n.y) * 0.002 * state.hot;
      if (state.drag !== n) { n.x += n.vx; n.y += n.vy; }
      n.vx *= 0.85; n.vy *= 0.85;
      n.x = Math.max(10, Math.min(w - 10, n.x));
      n.y = Math.max(10, Math.min(h - 10, n.y));
    }
    state.hot *= 0.985;
  }
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const radius = (n) => Math.min(4 + Math.sqrt(n.deg) * 1.5, 22);
  function draw() {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const line = cssVar('--line-2'), text = cssVar('--text'), dim = cssVar('--dim'),
      accent = cssVar('--accent'), panel = cssVar('--panel-3');
    for (const e of state.edges) {
      const hi = state.hover && (e.a === state.hover || e.b === state.hover);
      ctx.strokeStyle = hi ? accent : line;
      ctx.lineWidth = Math.min(1 + Math.log2(e.n), 6);
      ctx.beginPath();
      ctx.moveTo(e.a.x, e.a.y);
      ctx.lineTo(e.b.x, e.b.y);
      ctx.stroke();
      // Arrowhead pulled back to the destination node's rim — direction is
      // the entire point of a lateral-movement edge.
      const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const tx = e.b.x - (dx / d) * (radius(e.b) + 3), ty = e.b.y - (dy / d) * (radius(e.b) + 3);
      const ang = Math.atan2(dy, dx);
      ctx.fillStyle = hi ? accent : dim;
      ctx.beginPath();
      ctx.moveTo(tx, ty);
      ctx.lineTo(tx - 7 * Math.cos(ang - 0.4), ty - 7 * Math.sin(ang - 0.4));
      ctx.lineTo(tx - 7 * Math.cos(ang + 0.4), ty - 7 * Math.sin(ang + 0.4));
      ctx.fill();
    }
    const showLabels = state.nodes.size <= 80;
    ctx.font = `11px ${cssVar('--mono')}`;
    for (const n of state.nodes.values()) {
      const r = radius(n);
      ctx.fillStyle = panel;
      ctx.strokeStyle = n === state.hover ? accent : (n.out ? accent : line);
      ctx.lineWidth = n === state.hover ? 2 : 1.25;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      if (showLabels || n === state.hover) {
        ctx.fillStyle = n === state.hover ? text : dim;
        ctx.fillText(n.name, n.x + r + 4, n.y + 4);
      }
    }
  }

  // ---- interaction -------------------------------------------------------
  const pick = (mx, my) => {
    for (const n of state.nodes.values()) {
      const dx = mx - n.x, dy = my - n.y;
      if (dx * dx + dy * dy <= (radius(n) + 4) ** 2) return n;
    }
    return null;
  };
  const pos = (ev) => {
    const r = canvas.getBoundingClientRect();
    return [ev.clientX - r.left, ev.clientY - r.top];
  };
  canvas.onmousemove = (ev) => {
    const [mx, my] = pos(ev);
    if (state.drag) {
      state.drag.x = mx; state.drag.y = my;
      state.hot = Math.max(state.hot, 0.3);
      kick();
      return;
    }
    const n = pick(mx, my);
    if (n !== state.hover) { state.hover = n; kick(); }
    if (n) {
      tip.style.display = 'block';
      tip.style.left = `${mx + 12}px`;
      tip.style.top = `${my + 12}px`;
      tip.textContent = `${n.name} — ${n.deg.toLocaleString()} events`;
    } else tip.style.display = 'none';
  };
  canvas.onmousedown = (ev) => {
    const [mx, my] = pos(ev);
    state.drag = pick(mx, my);
    if (state.drag) kick();
  };
  window.addEventListener('mouseup', () => { state.drag = null; });
  new ResizeObserver(() => kick()).observe(wrap);
  repaint = () => { fillSources(); kick(); };
}

export function onShow() {
  // Sources may have been imported/dropped while another tab was active —
  // rebuild the table picker and repaint (the canvas was hidden, and a
  // theme switch may have changed the tokens it draws with).
  if (repaint) repaint();
}
