/* The chat pane both of this plugin's surfaces are built from — the
   Claude tab and the SQL Copilot panel. One transcript list, one
   composer, the history replay, the Clear confirmation; what differs is
   passed in: the conversation (`mode`, which is also which table in the
   case file), the intro line, extra request fields, how an answer is
   rendered, and what to do when one lands.

   Nothing is kept in this module: the pane renders what GET history
   returns and the server builds the API context from the same table. A
   chat history in a JS variable is one reload from gone.

   Imported relatively (`./chat.js`) by both entry modules. Note that a
   plugin reload cache-busts only the ENTRY module's URL; while iterating
   on this file, reload the browser tab too. */

export function mountChat(container, winnow, opts) {
  const { el, post } = winnow;
  const mode = opts.mode || 'chat';

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
  // An assistant bubble, rendered the surface's way (the Copilot lifts
  // SQL blocks out with buttons); plain text by default.
  const answer = (text) => {
    const d = line('assistant', '');
    if (opts.renderAnswer) opts.renderAnswer(d, text); else d.textContent = text;
    log.scrollTop = log.scrollHeight;
    return d;
  };
  const intro = () => line('assistant', opts.intro);

  // Whatever this case already said, before any network call.
  (async () => {
    try {
      const r = await winnow.api(`${winnow.base}/history?mode=${mode}`);
      if (r.turns.length) {
        for (const t of r.turns) { if (t.role === 'user') line('user', t.content); else answer(t.content); }
        if (!r.persisted) line('error', 'No case is open, so this conversation will not be kept.');
      } else {
        intro();
      }
    } catch (e) {
      intro();
      line('error', 'Could not load this case\'s chat: ' + e.message);
    }
  })();

  const bar = el('div');
  bar.style.cssText = 'flex:0 0 auto;display:flex;gap:8px;align-items:flex-end;padding:10px;border-top:1px solid var(--line-2);flex-wrap:wrap';
  const input = el('textarea');
  input.rows = 2;
  input.placeholder = opts.placeholder || 'Ask Claude…  (Enter to send, Shift+Enter for a newline)';
  input.style.cssText = 'flex:1 1 12em;min-width:0;resize:vertical;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:7px 9px;font:inherit';
  const send = el('button', 'btn', 'Send');
  let schemaCb = null;
  if (opts.schemaToggle !== false) {
    const schemaLabel = el('label', 'note-status');
    schemaCb = el('input');
    schemaCb.type = 'checkbox';
    schemaCb.checked = true;
    schemaLabel.append(schemaCb, document.createTextNode(' send schema'));
    schemaLabel.title = "Include the case's table/column names as context — column names only, never row data";
    bar.append(input, schemaLabel);
  } else {
    bar.append(input);
  }
  const stats = el('span', 'note-status', '');
  const clearBtn = el('button', 'btn ghost', 'Clear');
  clearBtn.title = 'Forget this conversation (it is stored in the case file)';
  clearBtn.onclick = async () => {
    if (!(await winnow.confirmDialog(opts.clearPrompt || 'Forget this conversation?', { danger: true, okLabel: 'Clear' }))) return;
    try {
      await post(`${winnow.base}/clear`, { mode });
      log.replaceChildren();
      intro();
    } catch (e) { winnow.toast('Could not clear it: ' + e.message, 6000); }
  };
  bar.append(send, clearBtn, stats);
  container.append(bar);

  async function submit() {
    const q = input.value.trim();
    if (!q || send.disabled) return;
    input.value = '';
    line('user', q);
    const pending = line('assistant', '…');
    send.disabled = true;
    try {
      // No history in the body: the server reads this conversation's
      // transcript, which is also what it appends this exchange to.
      const extra = opts.extraBody ? await opts.extraBody() : {};
      const r = await post(`${winnow.base}/ask`, {
        question: q,
        mode,
        schema: (!schemaCb || schemaCb.checked) ? winnow.schemaText() : null,
        ...extra,
      });
      pending.remove();
      const bubble = answer(r.answer || '(empty response)');
      const cached = r.usage.cache_read_input_tokens ? `, ${r.usage.cache_read_input_tokens.toLocaleString()} cached` : '';
      stats.textContent = `${r.model} · ${r.usage.input_tokens.toLocaleString()} in${cached} · ${r.usage.output_tokens.toLocaleString()} out`;
      if (opts.onAnswer) opts.onAnswer(r, bubble);
    } catch (e) {
      pending.remove();
      line('error', e.message);
      if (opts.onError) opts.onError(e);
    }
    send.disabled = false;
    input.focus();
  }
  send.onclick = submit;
  input.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
  };

  return { focus: () => input.focus(), line };
}
