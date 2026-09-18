/* Top values panel — the ten most common values of one column for the
   rows the grid is showing right now, refetched on every view change.

   A worked register_toolbar_panel: chrome from Winnow's own classes and
   tokens (btn ghost, note-status, --dim, --mono), data from the app's
   /api/group_summary through winnow.api (the header value picker's own
   query, scoped to the view), and winnow.onViewChange to follow the
   grid. Clicking a value copies it. */

const LIMIT = 10;
const REFRESH_MS = 150;

let refresh = null;   // module-level so onShow can re-run it

export default function mount(container, winnow) {
  const { el, api, toast } = winnow;
  let column = null;
  let timer = null;
  let seq = 0;          // request counter: only the newest answer lands

  /* ---------------------------------------------------------- chrome */
  const head = el('div');
  head.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 10px 0;font-size:11px;color:var(--dim)';
  const title = el('span', null, 'Top values');
  title.style.cssText = 'letter-spacing:.08em;text-transform:uppercase;font-size:10px';
  const colSel = el('select');
  colSel.title = 'Which column to count';
  colSel.style.cssText = 'font-size:11px;padding:1px 4px;max-width:220px';
  colSel.onchange = () => { column = colSel.value || null; schedule(); };
  const info = el('span', null, '');
  info.style.cssText = 'margin-left:auto;font-family:var(--mono)';
  head.append(title, colSel, info);
  const list = el('div', 'tv-list');
  list.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px 6px;padding:4px 10px 6px';
  // One line of DOM text for the empty states, so it wraps in a narrow
  // window instead of clipping.
  const note = el('div', 'note-status');
  note.style.cssText = 'padding:2px 10px 6px;white-space:normal';
  note.hidden = true;
  container.append(head, list, note);

  /* --------------------------------------------------------- helpers */
  function columns() {
    const src = winnow.state.sources.find((s) => s.id === winnow.state.sourceId);
    return src ? src.columns.map((c) => c.name) : [];
  }
  function pickColumn() {
    const cols = columns();
    if (!(column && cols.includes(column))) column = cols[0] || null;
    colSel.replaceChildren();
    for (const c of cols) { const o = el('option', null, c); o.value = c; colSel.append(o); }
    if (column) colSel.value = column;
    return column;
  }
  function showNote(text) {
    list.replaceChildren();
    list.hidden = true;
    note.textContent = text;
    note.hidden = false;
    info.textContent = '';
  }
  async function copy(value) {
    try {
      await navigator.clipboard.writeText(value);
      toast(value === '' ? 'Copied an empty value' : `Copied ${value}`);
    } catch {
      // No clipboard (a remote http:// page, or permission refused): the
      // value is on screen, so say so rather than fail silently.
      toast(`Could not copy — the value is ${value === '' ? 'empty' : value}`);
    }
  }

  /* ---------------------------------------------------------- render */
  function render(groups, total) {
    if (!groups.length) { showNote('No rows in this view.'); return; }
    note.hidden = true;
    list.hidden = false;
    list.replaceChildren();
    for (const g of groups) {
      const value = g.value == null ? '' : String(g.value);
      const b = el('button', 'btn ghost tv-value');
      b.style.cssText = 'font-size:11px;padding:1px 7px;max-width:280px;overflow:hidden;text-overflow:ellipsis';
      b.dataset.value = value;
      const n = el('span', null, ` ${g.count.toLocaleString()}`);
      n.style.cssText = 'color:var(--dim);font-family:var(--mono)';
      b.append(el('span', null, value === '' ? '(empty)' : value), n);
      b.title = `${value === '' ? '(empty)' : value} — ${g.count.toLocaleString()} rows in this view (click to copy)`;
      b.onclick = () => copy(value);
      list.append(b);
    }
    info.textContent = `${total.toLocaleString()} rows`;
  }

  /* --------------------------------------------------------- refresh */
  async function load() {
    const v = winnow.state.view;   // {view_id, row_count} — the grid's CURRENT view
    if (!v || !pickColumn()) { showNote('Open a table to see its top values.'); return; }
    const mine = ++seq;
    let r;
    try {
      r = await api(`/api/group_summary?view_id=${encodeURIComponent(v.view_id)}`
        + `&column=${encodeURIComponent(column)}&limit=${LIMIT}&order=count&bucket_datetime=false`);
    } catch (e) {
      if (mine !== seq) return;
      // A 409 is the view going mid-rebuild: the rebuild's own view
      // change refetches, so what is on screen stays. Anything else is
      // worth a line of text.
      if (e.status !== 409) showNote(`Could not count ${column}: ${e.message}`);
      return;
    }
    if (mine !== seq) return;
    render(r.groups || [], v.row_count);
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(load, REFRESH_MS); }

  winnow.onViewChange(() => schedule());
  refresh = load;
  load();
}

// The view may have changed while a page tab hid the strip.
export function onShow() { if (refresh) refresh(); }
