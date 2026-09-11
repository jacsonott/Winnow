/* Table histogram panel — bars of WHEN the current view's rows happened,
   drawn between the toolbar and the grid, following every filter.

   Layout is time-proportional (a bar's x/width come from its bucket's
   start/width over the view's span), so gaps in activity show as gaps
   rather than being squeezed out. A drag across the bars becomes the case
   timeframe filter via winnow.setTimeRange; the view rebuilds; the panel
   hears the view change and redraws over the narrowed span. Everything
   visual comes from Winnow's CSS tokens (accent, panel, dim, sel), read
   at draw time so a theme switch repaints correctly. */

const REFRESH_MS = 150;
const HEIGHT = 96;

let refresh = null;

export default function mount(container, winnow) {
  const { el, post } = winnow;
  let data = null;          // last histogram response
  let column = null;        // the datetime column being charted
  let brush = null;         // {x0, x1} while dragging, in canvas CSS px
  let timer = null;
  let inflight = 0;

  /* ---------------------------------------------------------- chrome */
  const bar = el('div');
  bar.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 10px 0;font-size:11px;color:var(--dim)';
  const title = el('span', null, 'Histogram');
  title.style.cssText = 'letter-spacing:.08em;text-transform:uppercase;font-size:10px';
  const colSel = el('select');
  colSel.title = 'Which datetime column to chart';
  colSel.style.cssText = 'font-size:11px;padding:1px 4px;max-width:220px';
  colSel.onchange = () => { column = colSel.value || null; schedule(); };
  const info = el('span', null, '');
  info.style.cssText = 'margin-left:auto;font-family:var(--mono)';
  const clearBtn = el('button', 'btn ghost', 'Clear timeframe');
  clearBtn.style.cssText = 'font-size:11px;padding:1px 7px';
  clearBtn.title = 'Remove the timeframe filter this panel set (the ⏱ filter in the toolbar)';
  clearBtn.onclick = () => winnow.clearTimeRange();
  bar.append(title, colSel, info, clearBtn);
  container.append(bar);

  const canvas = el('canvas');
  canvas.className = 'th-canvas';
  canvas.style.cssText = `display:block;width:100%;height:${HEIGHT}px;cursor:crosshair;user-select:none`;
  container.append(canvas);
  const hint = el('div', 'note-status', 'Drag across the bars to set the timeframe filter to that range.');
  hint.style.cssText = 'padding:0 10px 4px;font-size:10px';
  container.append(hint);
  // The empty states are one line of DOM text, not a sentence painted
  // onto a 96px canvas: text on the canvas neither wraps nor shrinks, so
  // a narrow window clipped it, and the panel kept its full chart height
  // (plus the drag hint, plus Clear) to say there was nothing to chart.
  const empty = el('div', 'note-status th-empty');
  empty.style.cssText = 'padding:2px 10px 6px;font-size:11px;white-space:normal';
  empty.hidden = true;
  container.append(empty);
  function showEmpty(text) {
    canvas.hidden = true;
    hint.hidden = true;
    clearBtn.hidden = true;
    empty.textContent = text;
    empty.hidden = false;
  }
  function showChart() {
    empty.hidden = true;
    canvas.hidden = false;
    hint.hidden = false;
    clearBtn.hidden = false;
  }

  /* --------------------------------------------------------- helpers */
  const tokens = () => {
    const cs = getComputedStyle(document.documentElement);
    return {
      accent: cs.getPropertyValue('--accent').trim() || '#d9a441',
      dim: cs.getPropertyValue('--dim').trim() || '#888',
      line: cs.getPropertyValue('--line').trim() || '#333',
      sel: cs.getPropertyValue('--sel').trim() || 'rgba(255,255,255,.1)',
      text: cs.getPropertyValue('--text').trim() || '#ddd',
      mono: cs.getPropertyValue('--mono').trim() || 'monospace',
    };
  };
  const pad2 = (n) => String(n).padStart(2, '0');
  const iso = (epoch) => {
    const d = new Date(epoch * 1000);
    return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} `
      + `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}`;
  };
  const humanBucket = (s) => (s % 86400 === 0 ? `${s / 86400}d` : s % 3600 === 0 ? `${s / 3600}h`
    : s % 60 === 0 ? `${s / 60}m` : `${s}s`);

  function datetimeCols() {
    const src = winnow.state.sources.find((s) => s.id === winnow.state.sourceId);
    return src ? src.columns.filter((c) => c.type === 'datetime').map((c) => c.name) : [];
  }
  function pickColumn() {
    const cols = datetimeCols();
    if (!(column && cols.includes(column))) {
      const tr = winnow.state.timeRange;
      column = tr && tr.column && cols.includes(tr.column) ? tr.column : (cols[0] || null);
    }
    colSel.replaceChildren();
    for (const c of cols) { const o = el('option', null, c); o.value = c; colSel.append(o); }
    if (column) colSel.value = column;
    colSel.hidden = !cols.length;
    return column;
  }

  /* Span used for the x axis: the view's [start, end + one bucket). */
  function span() {
    if (!data || !data.buckets.length) return null;
    const first = data.buckets[0][0];
    const last = data.buckets[data.buckets.length - 1][0] + data.bucket_seconds;
    return { t0: first, t1: Math.max(last, first + data.bucket_seconds) };
  }
  const xOf = (t, w) => { const s = span(); return ((t - s.t0) / (s.t1 - s.t0)) * w; };
  const tOf = (x, w) => { const s = span(); return s.t0 + (x / w) * (s.t1 - s.t0); };

  /* ------------------------------------------------------------ draw */
  function draw() {
    if (!column) {
      showEmpty('No datetime column in this table — derive one from a column header to chart it.');
      info.textContent = '';
      return;
    }
    if (!data || !data.total) {
      showEmpty(inflight ? 'Loading…' : 'No rows with a parsable timestamp in this view.');
      info.textContent = data ? '0 rows' : '';
      return;
    }
    showChart();   // before measuring: a hidden canvas has no width
    const w = canvas.clientWidth || container.clientWidth || 600;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(HEIGHT * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, HEIGHT);
    const t = tokens();
    ctx.font = `10px ${t.mono}`;
    const top = 6, bottom = HEIGHT - 16, plotH = bottom - top;
    const max = Math.max(...data.buckets.map((b) => b[1]));
    const s = span();
    const bw = Math.max(1, (data.bucket_seconds / (s.t1 - s.t0)) * w - 1);
    ctx.fillStyle = t.accent;
    for (const [b, n] of data.buckets) {
      const x = xOf(b, w);
      const h = Math.max(1, (n / max) * plotH);
      ctx.fillRect(x, bottom - h, bw, h);
    }
    // baseline + axis labels (start / end, and a middle tick)
    ctx.strokeStyle = t.line;
    ctx.beginPath(); ctx.moveTo(0, bottom + 0.5); ctx.lineTo(w, bottom + 0.5); ctx.stroke();
    ctx.fillStyle = t.dim;
    ctx.fillText(iso(s.t0), 4, HEIGHT - 4);
    const endLabel = iso(s.t1);
    ctx.fillText(endLabel, w - ctx.measureText(endLabel).width - 4, HEIGHT - 4);
    const mid = iso((s.t0 + s.t1) / 2);
    ctx.fillText(mid, w / 2 - ctx.measureText(mid).width / 2, HEIGHT - 4);
    // brush overlay
    if (brush) {
      const x0 = Math.min(brush.x0, brush.x1), x1 = Math.max(brush.x0, brush.x1);
      // Translucent: --sel is an opaque-enough panel tint, and painting it
      // over the plot hid the bars being selected — you were choosing a
      // range by covering up the thing you were choosing it from. The
      // outline stays fully opaque, so the edges are still exact.
      ctx.save();
      ctx.globalAlpha = 0.28;
      ctx.fillStyle = t.sel;
      ctx.fillRect(x0, top, x1 - x0, plotH);
      ctx.restore();
      ctx.strokeStyle = t.accent;
      ctx.strokeRect(x0 + 0.5, top + 0.5, x1 - x0, plotH);
      // What the drag would apply, while it is being dragged.
      const r = snapped();
      const label = `${iso(r.start)} — ${iso(r.end)}`;
      ctx.fillStyle = t.text;
      const lw = ctx.measureText(label).width;
      ctx.fillText(label, Math.max(2, Math.min(w - lw - 2, (x0 + x1) / 2 - lw / 2)), top + 10);
    }
    const tr = winnow.state.timeRange;
    info.textContent = `${data.total.toLocaleString()} rows · ${humanBucket(data.bucket_seconds)} buckets · max ${max.toLocaleString()}`
      + (tr && tr.enabled && (tr.start || tr.end) ? ' · timeframe on' : '');
  }

  /* Snapping. Ranges should read as clean clock times, which is why the
     drag is rounded outwards — but rounding to the CURRENT bar width made
     a smaller timeframe unselectable: with 6h bars, any drag inside one
     bar became that whole 6h bar, so the view never narrowed enough for
     the histogram to re-bucket and "zoom in" did nothing. The unit is
     picked from the drag itself instead: fine enough that the rounding
     cannot grow the range much, never finer than a second, and never
     coarser than the bar it was dragged over. The server re-buckets from
     whatever span it is given (Store.HISTOGRAM_BUCKETS), so a genuinely
     small range is what produces the finer bars. */
  const SNAP_UNITS = [1, 5, 15, 30, 60, 300, 600, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400];
  function snapUnit(seconds) {
    const cap = data ? data.bucket_seconds : 1;
    const want = Math.max(1, seconds / 12);   // rounding adds ≤ ~8% per end
    let unit = SNAP_UNITS[0];
    for (const u of SNAP_UNITS) { if (u <= want && u <= cap) unit = u; }
    return unit;
  }

  /* The range the current drag would apply: both ends rounded outwards
     with one unit chosen from the drag's own width. */
  function snapped() {
    if (!brush) return null;
    const w = canvas.clientWidth || 1;
    const t0 = tOf(Math.min(brush.x0, brush.x1), w);
    const t1 = tOf(Math.max(brush.x0, brush.x1), w);
    const u = snapUnit(Math.max(1, t1 - t0));
    return { start: Math.floor(t0 / u) * u, end: Math.ceil(t1 / u) * u };
  }

  /* ----------------------------------------------------------- brush */
  canvas.addEventListener('mousedown', (e) => {
    if (!data || !data.total) return;
    const r = canvas.getBoundingClientRect();
    brush = { x0: e.clientX - r.left, x1: e.clientX - r.left };
    draw();
    e.preventDefault();
  });
  canvas.addEventListener('mousemove', (e) => {
    const r = canvas.getBoundingClientRect();
    const x = e.clientX - r.left;
    if (brush) { brush.x1 = x; draw(); }
    if (data && data.total) {
      const w = canvas.clientWidth || 1;
      const tt = tOf(x, w);
      const b = data.buckets.find((bb) => tt >= bb[0] && tt < bb[0] + data.bucket_seconds);
      canvas.title = b ? `${iso(b[0])} — ${b[1].toLocaleString()} rows` : iso(tt);
    }
  });
  const endBrush = () => {
    if (!brush) return;
    const w = canvas.clientWidth || 1;
    const x0 = Math.max(0, Math.min(brush.x0, brush.x1)), x1 = Math.min(w, Math.max(brush.x0, brush.x1));
    const wasDrag = Math.abs(x1 - x0) > 3;
    const r = snapped();
    brush = null;
    draw();
    if (!wasDrag || !r) return;
    winnow.setTimeRange({ column, start: iso(r.start), end: iso(r.end) });
  };
  canvas.addEventListener('mouseup', endBrush);
  canvas.addEventListener('mouseleave', (e) => { if (brush) endBrush(e); });

  /* ---------------------------------------------------------- refresh */
  async function load() {
    const v = winnow.state.view;
    if (!v || !pickColumn()) { data = null; draw(); return; }
    inflight++;
    draw();
    // Ask for as many buckets as the canvas can actually show, rather than
    // a fixed 160: at 900px that was 5px slivers, and a zoomed-in view came
    // back just as dense as the one it zoomed out of, which is what made
    // narrowing the timeframe feel like it had changed nothing. ~7px a bar
    // is readable, and the server picks the nearest clean width from
    // Store.HISTOGRAM_BUCKETS for the span it is given.
    const maxBuckets = Math.max(20, Math.min(400, Math.floor((canvas.clientWidth || 600) / 7)));
    try { data = await post(`${winnow.base}/histogram`, { view_id: v.view_id, column, max_buckets: maxBuckets }); }
    catch { data = null; }   // a mid-rebuild 400 just means the next view change repaints
    inflight = Math.max(0, inflight - 1);
    draw();
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(load, REFRESH_MS); }

  winnow.onViewChange(() => schedule());
  // Tokens are read at draw time, so a skin/accent change is one redraw.
  winnow.onAppearanceChange(() => draw());
  window.addEventListener('resize', () => draw());
  refresh = load;
  load();
}

export function onShow() { if (refresh) refresh(); }
