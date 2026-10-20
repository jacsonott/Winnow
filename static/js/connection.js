/* Is Winnow's server still there? — and saying so plainly when it isn't.

   Winnow exits on its own a couple of minutes after the last window goes
   away (server.py's idle-shutdown block). That is the right behaviour for
   a tool launched by double-clicking a file, but it means a window left
   open across a laptop sleep, a VM suspend or a crash comes back to a
   page whose every click fails. Without this, that state showed up as a
   bare "Failed to fetch" toast on whatever the analyst happened to click,
   and views that swallow load errors looked simply empty — which reads as
   lost data rather than a stopped process.

   Two signals, because each catches what the other misses: the presence
   stream drops when the server goes (even if nobody clicks anything), and
   a failed request proves it (even if the stream is mid-retry). */
import { $, api, el, onConnectionChange } from './core.js';

// EventSource reconnects by itself every few seconds, so a single error is
// usually a blip — a restart, a proxy hiccup. Wait before crying wolf.
const GRACE_MS = 5000;

let stream = null;
let pending = null;
let down = false;
let shutDown = false;   // the server said it was going (onShutdown), not just vanished

function banner() { return $('connBanner'); }

function show() {
  if (down) return;
  down = true;
  const b = banner();
  if (!b) return;
  b.replaceChildren();
  const msg = el('span', 'conn-msg', shutDown
    ? 'Winnow’s server has shut down. Start Winnow again to carry on — your case file and '
      + 'everything saved in it are untouched.'
    : 'Lost the connection to Winnow’s server. It shuts down on its own a couple of minutes '
      + 'after the last window closes — if that happened, start Winnow again.');
  const retry = el('button', 'btn ghost', 'Try again');
  retry.onclick = async () => {
    retry.disabled = true;
    retry.textContent = 'Checking…';
    try {
      await api('/api/version');
      // Back up: reload rather than patch a page whose state went stale
      // while it was cut off.
      location.reload();
    } catch {
      retry.disabled = false;
      retry.textContent = 'Try again';
      msg.textContent = 'Still no answer from Winnow’s server. Start Winnow again to carry on — '
        + 'your case file and everything saved in it are untouched.';
    }
  };
  b.append(msg, retry);
  b.hidden = false;
}

function hide() {
  if (pending) { clearTimeout(pending); pending = null; }
  shutDown = false;   // a server is answering again
  if (!down) return;
  down = false;
  const b = banner();
  if (b) b.hidden = true;
}

/* Presence: the open connection is how the server knows a browser is still
   attached — it shuts itself down once every window is gone. No data
   flows over it; the connection itself is the message, which is exactly
   why its open/error edges are the earliest notice we get either way.
   The one exception is its last word: a server going down on purpose
   (Shut down in any window, Ctrl+C, the idle exit) ends every stream with
   a `shutdown` event, so the windows still open can say that rather than
   "lost the connection … a couple of minutes after the last window
   closes", which is not what happened. */
/* Tell the server this window is going, so it can tell a closed window
   from a suspended one. Without it the server sees the same thing either
   way — the presence stream stopping — and has to wait out a much longer
   fuse before assuming nobody is coming back.

   `persisted` means the page is heading into the back/forward cache and
   may be restored, which is not a goodbye. keepalive lets the request
   outlive the page; a plain fetch here is cancelled on unload.

   Both requests name this page load (PAGE_ID), because with two windows
   open the server has to know WHOSE goodbye it heard: the goodbye and the
   stream's end arrive in either order, and one window's goodbye standing
   when the other later went quiet put that survivor on the closed-window
   fuse (server.py _presence_close). */
export const PAGE_ID = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;

function sayGoodbye(e) {
  if (e && e.persisted) return;
  try {
    fetch(`/api/goodbye?page=${PAGE_ID}`, {
      method: 'POST', keepalive: true,
      headers: { 'X-Timeline-Lite-Client': '1' },
    }).catch(() => {});
  } catch { /* unload is a hostile place; never throw here */ }
}

/* Stop holding the presence stream, for the window that shut the server
   down: it is a static note from then on, with nothing to reconnect to.
   Every other window keeps its EventSource retrying, so it picks a
   restarted server back up on its own as it always has. */
export function closePresence() {
  if (stream) { stream.close(); stream = null; }
}

function onShutdown() {
  shutDown = true;
  if (pending) { clearTimeout(pending); pending = null; }
  // Repaint even if a request failed a moment earlier and already put
  // the lost-connection text up.
  down = false;
  show();
}

export function wireConnection() {
  // A request answered after the server said it was going is one it
  // finished on the way down, not a sign it is back; a stream that
  // reconnects (onopen) is.
  onConnectionChange((up) => {
    if (!up) show();
    else if (!shutDown) hide();
  });
  window.addEventListener('pagehide', sayGoodbye);
  stream = new EventSource(`/api/presence?page=${PAGE_ID}`);
  stream.addEventListener('shutdown', onShutdown);
  stream.onopen = hide;
  stream.onerror = () => {
    // readyState CLOSED means it gave up; CONNECTING means it is retrying,
    // and the server may simply be restarting — give that a moment.
    if (down || pending) return;
    pending = setTimeout(() => {
      pending = null;
      if (!stream || stream.readyState !== EventSource.OPEN) show();
    }, GRACE_MS);
  };
  return stream;
}
