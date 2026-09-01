/* Dependency-free canvas charts — the shared visualization surface the
   stack view, entity pivot, and dashboard widgets all draw through (see
   docs/design/analysis-suite.md). No chart library: airgap rule. Every
   color is read from Winnow's CSS tokens at draw time, so a theme switch
   just repaints.

   Declarations only (frontend-modules rule): callers pass a canvas and a
   plain spec; nothing here holds state or wires events.

   Two renderers:
     drawBars(canvas, { rows, label, value, color?, horizontal?, max?, onPick? })
       rows: [{...}]; label/value/color are KEY names into each row.
       horizontal bars (default) label well for long string values (host
       names, command lines); vertical for time-ordered counts.
     drawHistogram(canvas, { buckets, colors, brush? })
       buckets: [[key, [c0, c1, ...]], ...] sorted by key; one stacked bar
       per bucket, one segment per series index, colored by colors[i].
*/

const cssVar = (n, fallback) =>
  (getComputedStyle(document.documentElement).getPropertyValue(n).trim() || fallback);

function fit(canvas) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = canvas.clientWidth || 300, h = canvas.clientHeight || 150;
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr; canvas.height = h * dpr;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

export function drawBars(canvas, spec) {
  const { ctx, w, h } = fit(canvas);
  const rows = spec.rows || [];
  if (!rows.length) return { boxes: [] };
  const valKey = spec.value || 'n', labKey = spec.label || 'value';
  const accent = cssVar('--accent', '#d9a441');
  const line = cssVar('--line-2', '#333a35');
  const dim = cssVar('--dim', '#7c8578');
  const text = cssVar('--text', '#c9cdc4');
  const max = spec.max || Math.max(1, ...rows.map((r) => +r[valKey] || 0));
  const boxes = [];
  ctx.font = `11px ${cssVar('--mono', 'monospace')}`;

  if (spec.horizontal !== false) {
    // Horizontal: a labelled row per value — reads long strings cleanly.
    const rowH = Math.max(16, Math.min(30, h / rows.length));
    const labelW = Math.min(spec.labelWidth || 220, w * 0.5);
    const barMax = w - labelW - 54;
    rows.forEach((r, i) => {
      const y = i * rowH;
      if (y > h) return;
      const val = +r[valKey] || 0;
      ctx.fillStyle = text;
      ctx.textBaseline = 'middle';
      const lab = String(r[labKey] == null || r[labKey] === '' ? '(empty)' : r[labKey]);
      ctx.fillText(lab.length > 40 ? lab.slice(0, 39) + '…' : lab, 2, y + rowH / 2);
      const bw = Math.max(1, (val / max) * barMax);
      ctx.fillStyle = spec.color ? (r[spec.color] || accent) : accent;
      ctx.globalAlpha = 0.85;
      ctx.fillRect(labelW, y + 3, bw, rowH - 6);
      ctx.globalAlpha = 1;
      ctx.fillStyle = dim;
      ctx.textAlign = 'left';
      ctx.fillText(val.toLocaleString(), labelW + bw + 5, y + rowH / 2);
      boxes.push({ row: r, x: 0, y, w, h: rowH });
    });
  } else {
    const bw = w / rows.length;
    rows.forEach((r, i) => {
      const val = +r[valKey] || 0;
      const bh = (val / max) * (h - 16);
      ctx.fillStyle = spec.color ? (r[spec.color] || accent) : accent;
      ctx.fillRect(i * bw + 1, h - bh, Math.max(1, bw - 2), bh);
      boxes.push({ row: r, x: i * bw, y: 0, w: bw, h });
    });
    ctx.strokeStyle = line;
    ctx.beginPath(); ctx.moveTo(0, h - 0.5); ctx.lineTo(w, h - 0.5); ctx.stroke();
  }
  return { boxes };
}

export function drawHistogram(canvas, spec) {
  const { ctx, w, h } = fit(canvas);
  const buckets = spec.buckets || [];
  if (!buckets.length) return;
  const colors = spec.colors || [cssVar('--accent', '#d9a441')];
  const brush = spec.brush || null;
  const bw = w / buckets.length;
  const max = Math.max(1, ...buckets.map(([, row]) => row.reduce((a, b) => a + (+b || 0), 0)));
  buckets.forEach(([key, row], i) => {
    let y = h;
    const inBrush = !brush || (key >= brush[0] && key <= brush[1]);
    row.forEach((v, k) => {
      if (!v) return;
      const bh = ((+v || 0) / max) * (h - 6);
      ctx.fillStyle = colors[k] || cssVar('--dim', '#7c8578');
      ctx.globalAlpha = inBrush ? 1 : 0.3;
      ctx.fillRect(i * bw + 1, y - bh, Math.max(1, bw - 2), bh);
      y -= bh;
    });
  });
  ctx.globalAlpha = 1;
}

/* Map a horizontal-bar click back to its row, using the boxes drawBars
   returned. Callers keep the boxes from the last draw. */
export function pickBar(boxes, x, y) {
  for (const b of boxes || []) {
    if (x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h) return b.row;
  }
  return null;
}
