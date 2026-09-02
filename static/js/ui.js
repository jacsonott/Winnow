/* The shared UI primitives: the modal singleton, confirm/prompt dialogs, and
the one floating-menu implementation behind every dropdown and right-click.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, el } from './core.js';
import { setSearchAllRepaint } from './search.js';

/* ---------------------------------------------------------------- modal */

/* Which keymap action "owns" the modal that is currently showing — set
   by the openers below via markModalAction, consumed by modal(). The
   keymap's dialog guard uses it to make every keybind-openable dialog a
   TOGGLE: pressing C again closes the table menu, e the builder, and so
   on, however the dialog was opened. */
let pendingModalAction = null;
let modalAction = null;

export function markModalAction(action) { pendingModalAction = action; }

export function currentModalAction() { return modalAction; }

export function modal(title, build, opts = {}) {
  modalAction = pendingModalAction;
  pendingModalAction = null;
  $('modalTitle').textContent = title;
  document.querySelector('.modal-card').classList.toggle('wide', !!opts.wide);
  document.querySelector('.modal-card').classList.toggle('xwide', opts.wide === 'x');
  const b = $('modalBody');
  b.replaceChildren();
  // Any modal opening supersedes the Search-all pane's repaint hook; the
  // search-all builder re-installs its own below. (The background job keeps
  // running either way — only the painting stops.)
  setSearchAllRepaint(() => {});
  build(b);
  $('modal').hidden = false;
}

/* ------------------------------------------------------- date picker */

/* A 📅 button that opens a calendar for the timestamp input beside it and
   writes the pick back in the app's own shape (YYYY-MM-DD HH:MM:SS — see
   openTimeRangeModal's comment on why the text input itself must stay
   free-typed ISO). The calendar is OURS, not the browser's: Chromium's
   native popup only knows light/dark, never the app's five styles or the
   accent, so it read as a foreign object on every theme. This one is an
   anchoredPanel — the same machinery every menu uses — so it inherits the
   tokens, the z-order above modals, and the Escape/outside-click dismissal
   for free. Dependency-free, per the airgap rule. */

const DP_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                   'August', 'September', 'October', 'November', 'December'];

export function datePickerButton(input, { time = true } = {}) {
  const btn = el('button', 'btn ghost date-pick-btn', '📅');
  btn.type = 'button';
  btn.title = 'Pick from a calendar';
  btn.onclick = () => openDatePickerPanel(btn, input, time);
  return btn;
}

function openDatePickerPanel(anchor, input, withTime) {
  const pad = (n) => String(n).padStart(2, '0');
  // Seed from what's typed, when it's already the ISO shape — the month
  // shown and the highlighted day should be the value being edited.
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(input.value.trim());
  const today = new Date();
  const todayKey = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`;
  const selectedKey = m ? `${m[1]}-${m[2]}-${m[3]}` : null;
  let viewYear = m ? +m[1] : today.getFullYear();
  let viewMonth = m ? +m[2] - 1 : today.getMonth();

  anchoredPanel(anchor, 'date-pick-panel', (panel, close) => {
    let timeInput = null;
    const commit = (dateKey) => {
      const t = withTime && timeInput
        ? /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(timeInput.value.trim()) : null;
      const hms = withTime
        ? ' ' + (t ? `${pad(t[1])}:${t[2]}:${t[3] || '00'}` : '00:00:00') : '';
      input.value = dateKey + hms;
      // Fire the text input's own handlers (live previews, validation) —
      // a pick must be indistinguishable from typing.
      input.dispatchEvent(new Event('input', { bubbles: true }));
      close();
      input.focus();
    };

    const head = el('div', 'dp-head');
    const title = el('span', 'dp-title');
    const nav = (label, tip, dY, dM) => {
      const b = el('button', 'dp-nav', label);
      b.type = 'button';
      b.title = tip;
      b.onclick = (e) => {
        e.stopPropagation();       // a nav click must not read as outside-the-menu
        viewMonth += dM;
        viewYear += dY + Math.floor(viewMonth / 12);
        viewMonth = ((viewMonth % 12) + 12) % 12;
        paint();
      };
      return b;
    };
    head.append(nav('«', 'Previous year', -1, 0), nav('‹', 'Previous month', 0, -1),
                title, nav('›', 'Next month', 0, 1), nav('»', 'Next year', 1, 0));
    panel.append(head);

    const dow = el('div', 'dp-dow');
    for (const d of ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa']) dow.append(el('span', null, d));
    panel.append(dow);
    const grid = el('div', 'dp-grid');
    panel.append(grid);

    function paint() {
      title.textContent = `${DP_MONTHS[viewMonth]} ${viewYear}`;
      grid.replaceChildren();
      const first = new Date(viewYear, viewMonth, 1);
      const start = new Date(viewYear, viewMonth, 1 - first.getDay());   // back to Sunday
      for (let i = 0; i < 42; i++) {
        const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
        const key = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
        const cell = el('button', 'dp-day', String(d.getDate()));
        cell.type = 'button';
        cell.dataset.date = key;
        if (d.getMonth() !== viewMonth) cell.classList.add('other');
        if (key === todayKey) cell.classList.add('today');
        if (key === selectedKey) cell.classList.add('selected');
        cell.onclick = () => commit(key);
        grid.append(cell);
      }
    }
    paint();

    const foot = el('div', 'dp-foot');
    if (withTime) {
      timeInput = el('input', 'dp-time');
      timeInput.value = m && m[4] ? `${m[4]}:${m[5]}:${m[6] || '00'}` : '00:00:00';
      timeInput.title = 'The time of day a picked date is written with';
      timeInput.spellcheck = false;
      foot.append(timeInput);
    }
    const todayBtn = el('button', 'btn ghost dp-act', 'Today');
    todayBtn.type = 'button';
    todayBtn.onclick = () => commit(todayKey);
    foot.append(todayBtn);
    const clear = el('button', 'btn ghost dp-act', 'Clear');
    clear.type = 'button';
    clear.title = 'Empty the field';
    clear.onclick = () => {
      input.value = '';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      close();
      input.focus();
    };
    foot.append(clear);
    panel.append(foot);
  });
}

/* --------------------------------------------------------- confirm/prompt */

/* Replacements for window.confirm()/window.prompt() — native browser
   dialogs can't be restyled and look jarring next to the rest of the app.
   Both build their own overlay (not the #modal singleton) so they can be
   triggered from a click handler inside an already-open modal and stack
   visibly above it, then resolve a Promise once the user answers instead
   of blocking synchronously like the native versions do. */
export function _spawnDialog(build) {
  return new Promise((resolve) => {
    const overlay = el('div', 'confirm-overlay');
    const card = el('div', 'confirm-card');
    let settled = false;
    function close(result) {
      if (settled) return;
      settled = true;
      overlay.remove();
      document.removeEventListener('keydown', onKey, true);
      resolve(result);
    }
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close(build.cancelValue); }
    }
    // Same both-ends-on-the-backdrop rule as #modal above — prompt()
    // dialogs have inputs whose text gets drag-selected too.
    let pressOnOverlay = false;
    let releaseOnOverlay = false;
    overlay.onmousedown = (e) => { pressOnOverlay = e.target === overlay; };
    overlay.onmouseup = (e) => { releaseOnOverlay = e.target === overlay; };
    overlay.onclick = (e) => { if (e.target === overlay && pressOnOverlay && releaseOnOverlay) close(build.cancelValue); };
    build(card, close);
    overlay.append(card);
    document.body.append(overlay);
    document.addEventListener('keydown', onKey, true);
  });
}

export function confirmDialog(message, opts = {}) {
  const build = (card, close) => {
    card.append(el('p', 'confirm-message', message));
    const acts = el('div', 'confirm-actions');
    const cancelBtn = el('button', 'btn ghost', opts.cancelLabel || 'Cancel');
    cancelBtn.onclick = () => close(false);
    const okBtn = el('button', 'btn' + (opts.danger ? ' danger' : ''), opts.okLabel || 'OK');
    okBtn.onclick = () => close(true);
    acts.append(cancelBtn, okBtn);
    card.append(acts);
    setTimeout(() => okBtn.focus(), 0);
  };
  build.cancelValue = false;
  return _spawnDialog(build);
}

export function promptDialog(message, defaultValue = '', opts = {}) {
  const build = (card, close) => {
    card.append(el('p', 'confirm-message', message));
    const input = el('input');
    input.className = 'confirm-input';
    input.type = 'text';
    input.value = defaultValue || '';
    input.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); close(input.value); } };
    card.append(input);
    const acts = el('div', 'confirm-actions');
    const cancelBtn = el('button', 'btn ghost', 'Cancel');
    cancelBtn.onclick = () => close(null);
    const okBtn = el('button', 'btn', opts.okLabel || 'OK');
    okBtn.onclick = () => close(input.value);
    acts.append(cancelBtn, okBtn);
    card.append(acts);
    setTimeout(() => { input.focus(); input.select(); }, 0);
  };
  build.cancelValue = null;
  return _spawnDialog(build);
}

/* ------------------------------------------------------------ dropdown menu */

/* Minimal floating menu — one instance ever open at a time. Closes on
   outside click, Escape, or an item's own click (items are expected to
   open a modal/do their thing and don't need to close it themselves).

   Three surfaces share this machinery, differing only in what they're
   positioned against and what they hold: `dropdownMenu` (under a button —
   the Case menu, a column's ▾), `contextMenu` (at the pointer — the
   right-click menus on a row, a tab, a sidebar row) and `anchoredPanel` (a
   card with real controls in it — the header value picker). One
   implementation is what makes "only one of these is ever open, and Escape
   always closes it" true across all three instead of three near-copies of
   the same two listeners. */
export let openMenuEl = null;

export let openMenuAnchor = null;

export function closeMenu() {
  if (openMenuAnchor) openMenuAnchor.setAttribute('aria-expanded', 'false');
  if (openMenuEl) openMenuEl.remove();
  openMenuEl = null;
  openMenuAnchor = null;
  document.removeEventListener('mousedown', onMenuOutsideClick, true);
  document.removeEventListener('keydown', onMenuKeydown, true);
}

export function onMenuOutsideClick(e) { if (openMenuEl && !openMenuEl.contains(e.target)) closeMenu(); }

/* Swallowed rather than left to bubble: the document-level Escape handler
   further down clears the row selection, and dismissing a menu you just
   opened shouldn't also throw away what was selected underneath it. */
export function onMenuKeydown(e) {
  if (e.key !== 'Escape' || !openMenuEl) return;
  e.preventDefault();
  e.stopPropagation();
  closeMenu();
}

/* One item is {label, onclick} plus any of: `disabled`, `title`, `checked`
   (renders a ✓ column — pass false for "checkable but off", omit entirely
   for a plain item), `swatch` (a color chip, for tags), `hint` (right-aligned
   dim text, e.g. a hotkey) and `keepOpen` (re-render the menu in place
   instead of closing it, so toggling three tags is three clicks). '-' is a
   separator and {header} a section label. */
export function menuItemNode(item, rerender) {
  // menu-item-flex, not plain .menu-item: the ✓/swatch/hint slots need a
  // flex row, while the sidebar's own hand-built .menu-item rows (a bare
  // text node inside the button) still rely on block-level ellipsing.
  const b = el('button', 'menu-item menu-item-flex');
  if (item.checked !== undefined) b.append(el('span', 'menu-check', item.checked ? '✓' : ''));
  if (item.swatch) {
    const sw = el('span', 'menu-swatch');
    sw.style.background = item.swatch;
    b.append(sw);
  }
  b.append(el('span', 'menu-item-text', item.label));
  if (item.hint) b.append(el('span', 'menu-item-hint', item.hint));
  b.disabled = !!item.disabled;
  if (item.title) b.title = item.title;
  b.onclick = async () => {
    if (!item.keepOpen) { closeMenu(); item.onclick(); return; }
    await item.onclick();
    rerender();
  };
  return b;
}

export function fillMenuNode(menu, items, rerender) {
  menu.replaceChildren();
  for (const item of items) {
    if (!item) continue;
    if (item === '-') { menu.append(el('div', 'menu-sep')); continue; }
    if (item.header) {
      menu.append(el('div', 'menu-header' + (item.literal ? ' menu-header-literal' : ''), item.header));
      continue;
    }
    menu.append(menuItemNode(item, rerender));
  }
}

/* Positions a floating node against a rect — a button's own bounding box,
   or a zero-size rect at the pointer. Flips above the rect when there isn't
   room below (right-clicking a row near the bottom of the grid is the
   common case, not the edge case) and clamps into the viewport on both
   axes. Measured after the node is in the DOM, so callers append first. */
export function placeFloating(node, rect) {
  const m = 8;
  const w = node.offsetWidth, h = node.offsetHeight;
  let top = rect.bottom + 4;
  if (top + h > window.innerHeight - m) {
    const above = rect.top - h - 4;
    top = above >= m ? above : Math.max(m, window.innerHeight - h - m);
  }
  let left = rect.left;
  if (left + w > window.innerWidth - m) left = rect.right - w;
  node.style.top = Math.max(m, top) + 'px';
  node.style.left = Math.max(m, left) + 'px';
}

export function showFloating(node, rect, anchorEl) {
  document.body.append(node);
  placeFloating(node, rect);
  openMenuEl = node;
  openMenuAnchor = anchorEl || null;
  if (anchorEl) anchorEl.setAttribute('aria-expanded', 'true');
  // keydown arms NOW: the deferred attach exists so the mousedown that
  // opened the menu can't instantly close it via onMenuOutsideClick — a
  // self-trigger risk only a mouse event has. Deferring keydown too left a
  // one-tick window where Escape missed the menu entirely and fell through
  // to the app handler (clearing the selection under a still-open menu) —
  // real on a slow machine, caught by CI's browser job.
  document.addEventListener('keydown', onMenuKeydown, true);
  setTimeout(() => {
    document.addEventListener('mousedown', onMenuOutsideClick, true);
  }, 0);
}

/* `items` may be a function returning the array — that's what a keepOpen
   item re-runs to repaint itself with fresh state (a tag's ✓ after the
   tag actually landed), so callers build items from live state rather than
   patching DOM nodes by hand. */
export function showMenu(items, rect, anchorEl) {
  const get = typeof items === 'function' ? items : () => items;
  const menu = el('div', 'menu');
  const rerender = () => { if (openMenuEl === menu) fillMenuNode(menu, get(), rerender); };
  fillMenuNode(menu, get(), rerender);
  showFloating(menu, rect, anchorEl);
  return menu;
}

export function dropdownMenu(anchorEl, items) {
  const wasOpenForSameAnchor = openMenuAnchor === anchorEl;
  closeMenu();
  if (wasOpenForSameAnchor) return; // second click on the same anchor just toggles it shut
  showMenu(items, anchorEl.getBoundingClientRect(), anchorEl);
}

/* Right-click menus. `e` is the contextmenu event — its client coords are
   the anchor, and preventDefault() is the caller's job (some surfaces want
   the browser's own menu when the click misses anything actionable). */
export function contextMenu(e, items) {
  closeMenu();
  showMenu(items, { top: e.clientY, bottom: e.clientY, left: e.clientX, right: e.clientX });
}

/* A floating card that isn't a list of buttons — same one-at-a-time,
   outside-click and Escape behaviour, arbitrary contents. `build` gets the
   panel node and the close function. */
export function anchoredPanel(anchorEl, cls, build) {
  const wasOpenForSameAnchor = openMenuAnchor === anchorEl;
  closeMenu();
  if (wasOpenForSameAnchor) return null;
  const panel = el('div', 'menu ' + cls);
  build(panel, closeMenu);
  showFloating(panel, anchorEl.getBoundingClientRect(), anchorEl);
  return panel;
}

/* DOM wiring for this module, called once by main.js. Handlers can't
   fire during load, so the order these run in doesn't matter — the
   startup steps that DO depend on order live in main.js instead. */
export function wireUi() {
$('modalClose').onclick = () => ($('modal').hidden = true);

/* Backdrop close must key off where the press STARTED, not where the click
   resolves: a `click` fires on the common ancestor of its mousedown and
   mouseup targets, so drag-selecting text in a modal input and releasing
   past the card's edge lands the click on the backdrop — which used to
   close the modal out from under a half-copied path. */
let modalPressOnBackdrop = false;
let modalReleaseOnBackdrop = false;
$('modal').onmousedown = (e) => { modalPressOnBackdrop = e.target === $('modal'); };
$('modal').onmouseup = (e) => { modalReleaseOnBackdrop = e.target === $('modal'); };
$('modal').onclick = (e) => {
  // Both ends of the gesture must land on the backdrop. The press guard
  // (see its comment above) covers a select-drag OUT of the card; a drag
  // that STARTS on the backdrop and releases inside the card also
  // resolves its click to the backdrop (common-ancestor rule) and used to
  // close the Search-all modal mid-highlight. A deliberate backdrop click
  // is down+up in place and still closes.
  if (e.target === $('modal') && modalPressOnBackdrop && modalReleaseOnBackdrop) $('modal').hidden = true;
};
}
