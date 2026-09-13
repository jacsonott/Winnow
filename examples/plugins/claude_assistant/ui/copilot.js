/* The SQL Copilot — this plugin's page panel beside the SQL pane
   (register_page_panel). The same chat as the tab (chat.js) in "sql"
   mode: Claude answers with one fenced SQL block, and this module lifts
   every such block out of the answer and puts Insert and Run under it,
   wired to winnow.sqlPage. The editor's current text goes with every
   question, so "make this faster" is about the query on screen.

   As a reference: this is what the page-panel hook is for — a plugin that
   works WITH a built-in page rather than beside it. */

import { mountChat } from './chat.js';

let chat = null;
const SQL_BLOCK = /```sql\s*\n([\s\S]*?)```/gi;

export default function mount(container, winnow) {
  const { el } = winnow;

  // Split an answer into text and SQL blocks; each block gets its buttons.
  const renderAnswer = (bubble, text) => {
    const parts = [];
    let last = 0;
    SQL_BLOCK.lastIndex = 0;
    for (let m = SQL_BLOCK.exec(text); m; m = SQL_BLOCK.exec(text)) {
      parts.push({ text: text.slice(last, m.index) });
      parts.push({ sql: m[1].trim() });
      last = m.index + m[0].length;
    }
    parts.push({ text: text.slice(last) });
    for (const p of parts) {
      if (p.sql === undefined) {
        const t = p.text.trim();
        if (t) bubble.append(el('div', null, t));
        continue;
      }
      const pre = el('pre');
      pre.style.cssText = 'margin:6px 0 4px;padding:6px 8px;background:var(--ink);border:1px solid var(--line-2);font-family:var(--mono);font-size:12px;white-space:pre-wrap;overflow:auto';
      pre.textContent = p.sql;
      const acts = el('div');
      acts.style.cssText = 'display:flex;gap:6px;align-items:center;margin-top:4px';
      const insert = el('button', 'btn ghost', 'Insert');
      insert.title = 'Put this query in the editor (replaces the active query tab)';
      const run = el('button', 'btn', 'Run');
      run.title = 'Put this query in the editor and run it';
      const status = el('span', 'note-status', '');
      insert.onclick = async () => {
        try { await winnow.sqlPage.setText(p.sql); status.textContent = 'inserted'; }
        catch (e) { winnow.toast('Could not insert: ' + e.message, 6000); }
      };
      run.onclick = async () => {
        status.textContent = 'running…';
        run.disabled = true;
        try {
          await winnow.sqlPage.setText(p.sql);
          const r = await winnow.sqlPage.run();
          status.textContent = `${r.rows.length.toLocaleString()} rows${r.truncated ? ' (truncated)' : ''} · ${r.elapsed_ms} ms`;
        } catch (e) {
          status.textContent = '';
          chat.line('error', e.message);   // the SQL error, in the conversation where the query came from
        }
        run.disabled = false;
      };
      acts.append(insert, run, status);
      bubble.append(pre, acts);
    }
  };

  chat = mountChat(container, winnow, {
    mode: 'sql',
    intro: 'Ask for a query — "failed logons followed by a process launch within a minute" — '
      + 'or about the one in the editor: "why no rows?", "add the host", "make this faster". '
      + 'Answers come as SQL you can insert or run here. I see the schema and the editor, never the rows.',
    placeholder: 'Ask for a query…  (Enter to send)',
    clearPrompt: 'Forget this case\'s Copilot conversation?',
    schemaToggle: false,   // a copilot without the schema cannot write a query
    extraBody: async () => ({ current_sql: await winnow.sqlPage.text() }),
    renderAnswer,
  });
}

export function onShow() {
  if (chat) chat.focus();
}
