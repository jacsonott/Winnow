/* Claude assistant tab — a schema-aware chat pane. The interesting parts
   as a plugin-UI reference: winnow.schemaText() (the same LLM-ready schema
   dump the SQL pane's copy button builds) sent as context, chat history
   kept in the module (survives tab switches; reset on case switch when the
   whole mount is torn down), and errors surfaced inline in the log rather
   than only as toasts — a failed question is part of the conversation. */

let history = []; // [{role: 'user'|'assistant', content}] — what gets replayed to the API

export default function mount(container, winnow) {
  const { el, post } = winnow;

  const log = el('div');
  log.style.cssText = 'flex:1 1 auto;min-height:0;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px';
  container.append(log);

  const line = (role, text) => {
    const d = el('div');
    d.style.cssText = 'max-width:56em;white-space:pre-wrap;padding:8px 12px;border:1px solid var(--line-2);'
      + (role === 'user'
        ? 'align-self:flex-end;background:var(--sel);'
        : role === 'error'
          ? 'align-self:stretch;color:var(--danger);border-color:var(--danger-border);background:var(--danger-bg);'
          : 'align-self:flex-start;background:var(--panel-2);');
    d.textContent = text;
    log.append(d);
    log.scrollTop = log.scrollHeight;
    return d;
  };

  line('assistant',
    'Ask about the open case — event IDs, artifacts, or "write me a query for…". '
    + 'With the schema box checked I can see your tables\' columns (never the rows) '
    + 'and write SQL you can paste into the SQL pane.');

  const bar = el('div');
  bar.style.cssText = 'flex:0 0 auto;display:flex;gap:8px;align-items:flex-end;padding:10px;border-top:1px solid var(--line-2)';
  const input = el('textarea');
  input.rows = 2;
  input.placeholder = 'Ask Claude…  (Enter to send, Shift+Enter for a newline)';
  input.style.cssText = 'flex:1;resize:vertical;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:7px 9px;font:inherit';
  const send = el('button', 'btn', 'Send');
  const schemaLabel = el('label', 'note-status');
  const schemaCb = el('input');
  schemaCb.type = 'checkbox';
  schemaCb.checked = true;
  schemaLabel.append(schemaCb, document.createTextNode(' send schema'));
  schemaLabel.title = "Include the case's table/column names as context — column names only, never row data";
  const stats = el('span', 'note-status', '');
  bar.append(input, schemaLabel, send, stats);
  container.append(bar);

  async function submit() {
    const q = input.value.trim();
    if (!q || send.disabled) return;
    input.value = '';
    line('user', q);
    const pending = line('assistant', '…');
    send.disabled = true;
    try {
      const r = await post(`${winnow.base}/ask`, {
        question: q,
        history,
        schema: schemaCb.checked ? winnow.schemaText() : null,
      });
      pending.textContent = r.answer || '(empty response)';
      history.push({ role: 'user', content: q }, { role: 'assistant', content: r.answer });
      const cached = r.usage.cache_read_input_tokens ? `, ${r.usage.cache_read_input_tokens.toLocaleString()} cached` : '';
      stats.textContent = `${r.model} · ${r.usage.input_tokens.toLocaleString()} in${cached} · ${r.usage.output_tokens.toLocaleString()} out`;
    } catch (e) {
      pending.remove();
      line('error', e.message);
    }
    send.disabled = false;
    input.focus();
  }
  send.onclick = submit;
  input.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
  };
}

export function onShow(container) {
  const input = container.querySelector('textarea');
  if (input) input.focus();
}
