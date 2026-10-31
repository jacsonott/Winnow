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
import { $, api, el, noticeServerCase, onCaseChanged, onConnectionChange, setCaseToken } from './core.js';
import { followCaseSwitch, inPopout } from './popout.js';

// EventSource reconnects by itself every few seconds, so a single error is
// usually a blip — a restart, a proxy hiccup. Wait before crying wolf.
const GRACE_MS = 5000;

let stream = null;
let pending = null;
let down = false;
let shutDown = false;   // the server said it was going (onShutdown), not just vanished
let restarting = false; // waiting for a restarted server (awaitRestart)

function banner() { return $('connBanner'); }

function show() {
  // A restart's own screen already says the server is away, and why.
  if (down || restarting) return;
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

/* The server is restarting (Settings → Updates; server.py api_restart), so
   this window waits for it rather than reporting it lost. The new server
   reopens the same case on the same port, so a reload is all it takes to
   be back where the analyst was, running the new code. Told apart from
   the old server by `boot`, not the pid: a restart on Linux or macOS
   replaces the process and keeps its pid. Covers the page, as caseGone
   does: nothing on it can be saved until the server is back.

   The window that asked and every other window (by the presence stream's
   `restart` event) all come here; the first call is the one that waits. */
const RESTART_POLL_MS = 500;
const RESTART_GIVE_UP_MS = 90000;

export function awaitRestart(from) {
  if (restarting) return;
  restarting = true;
  if (pending) { clearTimeout(pending); pending = null; }
  const b = banner();
  if (b) b.hidden = true;
  const box = $('restarting');
  const title = el('div', 'case-gone-title', 'Restarting Winnow…');
  title.id = 'restartingTitle';
  const waiting = 'This window reloads by itself as soon as Winnow is back, with the same case open.';
  const msg = el('p', null, waiting);
  const card = el('div', 'case-gone-card restart-card');
  card.append(title, msg);
  if (box) { box.replaceChildren(card); box.hidden = false; }

  let deadline = Date.now() + RESTART_GIVE_UP_MS;
  const giveUp = () => {
    title.textContent = 'Winnow hasn’t come back';
    msg.textContent = 'It may still be starting, or the new version may not start. Start Winnow '
      + 'again the way you usually do — your case file and everything saved in it are untouched. '
      + 'If it won’t start, "python update.py --rollback" in the Winnow folder puts the previous '
      + 'version back. This window reloads by itself if Winnow comes back here.';
    const again = el('button', 'btn primary', 'Keep waiting');
    again.onclick = () => {
      again.remove();
      title.textContent = 'Restarting Winnow…';
      msg.textContent = waiting;
      deadline = Date.now() + RESTART_GIVE_UP_MS;
    };
    card.append(again);
  };
  const tick = async () => {
    // Its own timeout: while the old server stops, a request can sit in
    // the listening socket's queue for the new one to pick up.
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 5000);
    try {
      const r = await fetch('/api/version', { cache: 'no-store', signal: ctl.signal });
      const v = r.ok ? await r.json() : null;
      if (v && v.boot && v.boot !== from) { location.reload(); return; }
    } catch { /* not back yet */ } finally { clearTimeout(t); }
    if (deadline && Date.now() > deadline) { deadline = 0; giveUp(); }
    setTimeout(tick, RESTART_POLL_MS);
  };
  setTimeout(tick, RESTART_POLL_MS);
}

/* Another window switched this server to a different case (or the server
   came back with another one open): everything this page shows and holds —
   source ids, rids, tag ids, views — belongs to a case the server no
   longer has. The server already refuses every request from here (the case
   guard in server.py); this says so, and the only way on is a reload,
   which picks up whatever case is open now. Covers the page rather than
   sitting in a corner: a page that can't save anything must not look like
   one that can. On the home screen nothing of the old case is showing, so
   the page just stops speaking for it — the next case opened there sets a
   new one. */
function caseGone(now) {
  // A pop-out follows the case rather than covering itself: it is one page
  // of the main window, which switched (popout.js). The reload boots it on
  // whatever case is open now, and says so if its page isn't in that one.
  if (inPopout()) { followCaseSwitch(); return; }
  if (!$('home').hidden) { setCaseToken(null); return; }
  const box = $('caseGone');
  if (!box) return;
  const mine = ($('brandLabel') && $('brandLabel').textContent) || 'its case';
  const theirs = now ? `“${now.name}”` : 'no case';
  const card = el('div', 'case-gone-card');
  const title = el('div', 'case-gone-title', 'This window is out of date');
  title.id = 'caseGoneTitle';
  const reload = el('button', 'btn primary', 'Reload this window');
  reload.onclick = () => location.reload();
  card.append(
    title,
    el('p', null, `It was showing “${mine}”, but Winnow now has ${theirs} open — another `
      + 'window on this Winnow opened it. Nothing more from this window is saved, so none of it can '
      + 'land in the wrong case.'),
    el('p', null, 'To work on two cases side by side, start Winnow a second time and open the other '
      + 'case there: each one runs on its own and has its own window.'),
    reload);
  box.replaceChildren(card);
  box.hidden = false;
  reload.focus();
}

export function wireConnection() {
  // A request answered after the server said it was going is one it
  // finished on the way down, not a sign it is back; a stream that
  // reconnects (onopen) is.
  onConnectionChange((up) => {
    if (!up) show();
    else if (!shutDown) hide();
  });
  onCaseChanged(caseGone);
  window.addEventListener('pagehide', sayGoodbye);
  stream = new EventSource(`/api/presence?page=${PAGE_ID}`);
  stream.addEventListener('shutdown', onShutdown);
  stream.addEventListener('restart', (e) => awaitRestart(e.data));
  // Which case the server has open: said when the stream opens (so a
  // reconnect to a server restarted on another case is caught at once)
  // and whenever another window switches it.
  stream.addEventListener('case', (e) => {
    let now;
    try { now = JSON.parse(e.data); } catch { return; }
    noticeServerCase(now && now.token ? now : null);
  });
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
