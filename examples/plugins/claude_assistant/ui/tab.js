/* Claude assistant tab — a schema-aware chat pane. The interesting parts
   as a plugin-UI reference: winnow.schemaText() (the same LLM-ready schema
   dump the SQL pane's copy button builds) sent as context, a transcript the
   SERVER keeps in the case file (so it survives a reload, a case handed to
   someone else, and the service being unreachable), and errors surfaced
   inline in the log rather than only as toasts — a failed question is part
   of the conversation.

   An answer that arrives while the analyst is on another page announces
   itself through winnow.notify — a row in the jobs panel with a button
   back to this tab — rather than being found later, or missed.

   The pane itself is shared with the SQL Copilot panel: see chat.js. */

import { mountChat } from './chat.js';

let chat = null;

export default function mount(container, winnow) {
  // The notice's button brings the analyst back here — reopening the tab
  // if it had been closed from the strip.
  const announce = (title, detail) => winnow.notify({
    title, detail, actions: [{ label: 'Open Claude', onClick: () => winnow.showTab() }],
  });

  chat = mountChat(container, winnow, {
    mode: 'chat',
    intro: 'Ask about the open case — event IDs, artifacts, or "write me a query for…". '
      + 'With the schema box checked I can see your tables\' columns (never the rows) '
      + 'and write SQL you can paste into the SQL pane — or open the Copilot beside the pane to run it from there.',
    clearPrompt: 'Forget this case\'s conversation with Claude?',
    onAnswer: (r) => {
      if (container.hidden) announce('Claude answered', (r.answer || '').split('\n')[0].slice(0, 160)).done();
    },
    onError: (e) => {
      if (container.hidden) announce('Claude', e.message).fail();
    },
  });
}

export function onShow() {
  if (chat) chat.focus();
}
