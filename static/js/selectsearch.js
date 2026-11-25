/* Type to search in a drop-down list. Every <select> with more than five
   options — in the app, and in a plugin, which shares the document — opens
   this instead of the browser's own list: a search box over the options
   that narrows them as the analyst types.

   Why one document-level handler rather than a component each caller
   builds: there are dozens of <select>s across the app and the example
   plugins, every one made with el('select') and read with .value and
   onchange. Wrapping each would be an edit per call site, an API every
   plugin author has to learn, and the next select added would ship without
   it. Taking over the gesture that OPENS the list leaves the <select> where
   it is — its value, its change event, Playwright's select_option, every
   contextual style rule — and replaces only the list it drops down.

   What opens it: a press on the select (the browser opens its own list on
   mousedown, so preventing that one event is all it takes), and from the
   keyboard Alt+↓, Alt+↑, F4 and Space, the keys that open the native list,
   plus any printable key, which starts the search with that letter. The
   native list jumped to the first option STARTING with a typed letter;
   this finds every option CONTAINING every typed word, in any order, which
   is what a list of 300 column names needs.

   In the list: ↑/↓ and PgUp/PgDn move, Enter takes the highlighted option
   (the top hit while typing, so a few letters and Enter is the whole
   gesture), Escape closes it and changes nothing, Tab closes it and moves
   on. Picking sets the select and fires `input` and `change` on it, as the
   native list does, and only when the value really changed.

   The list is a .menu, so it wears each skin the way every menu does
   (docs/notes/ui.md, "Theming rule for controls"). It is appended to the
   body, not beside the select, for two reasons: a select inside a <label>
   would have every click on the list's padding forwarded to it, and a
   transformed ancestor would turn position: fixed into "fixed to that
   ancestor". Being outside the select's own container is why the
   mousedown and keydown listeners sit on window, in the capture phase:
   they run before a menu or panel that holds the select (onMenuOutsideClick,
   onMenuKeydown in ui.js) can read a press in the list as a click outside
   itself, and before the app's Escape handler can close the dialog under
   the list.

   `data-search="off"` on a select keeps the browser's list; `"on"` asks
   for the search however short the list is. */
import { el } from './core.js';
import { closeMenu, openMenuEl } from './ui.js';

/* More options than this, and the list gets a search box. */
export const SEARCH_ABOVE = 5;

/* Painting is capped, not the search: a select of thousands still finds
   everything, it just asks for another letter before drawing it all. */
const RENDER_CAP = 500;
const MAX_HEIGHT = 380;
const IS_MAC = /Mac/.test(navigator.platform || '');

let open = null;   // {sel, pop, input, list, note, entries, shown, active}
let seq = 0;

/* Whether this select gets the search. Only a drop-down: a list box
   (multiple, size > 1) already shows its options, and a disabled select
   opens nothing. Options hidden with `hidden` (or inside a hidden
   optgroup) aren't counted, since they aren't offered. */
export function isSearchable(sel) {
  if (!sel || sel.tagName !== 'SELECT') return false;
  if (sel.multiple || sel.size > 1 || sel.disabled) return false;
  if (sel.dataset.search === 'off') return false;
  if (sel.dataset.search === 'on') return true;
  return optionEntries(sel).length > SEARCH_ABOVE;
}

/* The words a query is made of. Every one has to appear somewhere in an
   option for it to match, in any order — the rule the derived-column side
   list's search and the column picker already use. */
export function queryTerms(query) {
  return String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
}

/* What a search reads for one option: its label, its optgroup's label
   (so "your" finds the saved formats under "Your formats"), and its value
   when that says something the label doesn't — an operation id like
   unix_seconds, with the underscores read as spaces. A value that is only
   digits is a table or row id, and matching "1" against every id that
   contains a 1 would bury the hits the analyst means. */
export function optionHaystack(label, group, value) {
  const v = String(value == null ? '' : value);
  const useValue = v && v !== label && !/^\d+$/.test(v);
  return [label, group || '', useValue ? v.replace(/_/g, ' ') : ''].join(' ').toLowerCase();
}

function optionEntries(sel) {
  const out = [];
  const add = (o, group, groupDisabled) => {
    if (o.hidden) return;
    const label = o.label;
    out.push({ option: o, label, group, disabled: o.disabled || groupDisabled,
               hay: optionHaystack(label, group, o.value) });
  };
  for (const node of sel.children) {
    if (node.tagName === 'OPTION') add(node, null, false);
    else if (node.tagName === 'OPTGROUP' && !node.hidden) {
      for (const o of node.children) if (o.tagName === 'OPTION') add(o, node.label, node.disabled);
    }
  }
  return out;
}

/* The label with each matched word marked, so the eye lands on why an
   option is there. Skipped for a label whose lowercase is a different
   length (a handful of Unicode letters), where the offsets would drift. */
function marked(label, terms) {
  const low = label.toLowerCase();
  if (!terms.length || low.length !== label.length) return [document.createTextNode(label)];
  const hit = new Uint8Array(label.length);
  for (const t of terms) {
    for (let i = low.indexOf(t); i >= 0; i = low.indexOf(t, i + t.length)) hit.fill(1, i, i + t.length);
  }
  const out = [];
  for (let i = 0; i < label.length;) {
    let j = i;
    while (j < label.length && hit[j] === hit[i]) j++;
    const part = label.slice(i, j);
    out.push(hit[i] ? el('mark', 'select-search-hit', part) : document.createTextNode(part));
    i = j;
  }
  return out;
}

/* What the select is called, for the search box's accessible name. Most
   selects sit inside their <label>, whose textContent would include every
   option's text too, so only the label's own parts are read. */
function selectName(sel) {
  const aria = sel.getAttribute('aria-label');
  if (aria) return aria;
  const label = sel.labels && sel.labels[0];
  let text = '';
  if (label) {
    for (const n of label.childNodes) if (!n.contains(sel)) text += n.textContent;
  }
  return text.replace(/\s+/g, ' ').trim() || sel.title || '';
}

export function openSelectSearch(sel, initial = '') {
  closeSelectSearch();
  if (!sel || !sel.isConnected) return;
  // One floating thing at a time — unless the select lives IN the open
  // menu or panel (the timeframe card, a histogram panel), which has to
  // stay up for its select to be there at all.
  if (openMenuEl && !openMenuEl.contains(sel)) closeMenu();
  const entries = optionEntries(sel);
  const id = `selectSearch${++seq}`;
  const pop = el('div', 'menu select-search');
  const input = el('input', 'vp-search select-search-box');
  input.type = 'text';
  input.autocomplete = 'off';
  input.spellcheck = false;
  input.placeholder = `Search ${entries.length.toLocaleString()} options…`;
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-expanded', 'true');
  input.setAttribute('aria-autocomplete', 'list');
  input.setAttribute('aria-controls', id);
  const name = selectName(sel);
  input.setAttribute('aria-label', name ? `Search ${name}` : 'Search the options');
  const list = el('div', 'select-search-list');
  list.id = id;
  list.setAttribute('role', 'listbox');
  const note = el('div', 'vp-note select-search-note');
  note.hidden = true;
  pop.append(input, list, note);
  document.body.append(pop);
  sel.classList.add('select-search-open');
  open = { sel, pop, input, list, note, entries, shown: [], active: -1 };
  input.value = initial;
  paint(true);
  place();
  input.focus({ preventScroll: true });
  // Put the caret after a letter the search was opened with.
  input.setSelectionRange(input.value.length, input.value.length);
  input.addEventListener('input', () => paint(false));
  document.addEventListener('scroll', onScroll, true);
  window.addEventListener('resize', onResize);
  document.addEventListener('winnow:modalclose', onModalClose);
}

/* `refocus`: put the focus back on the select, as closing the native list
   does. Also done whenever the focus was in the list, so a close never
   leaves the caret nowhere. */
export function closeSelectSearch({ refocus = false } = {}) {
  if (!open) return;
  const { sel, pop } = open;
  open = null;
  const hadFocus = pop.contains(document.activeElement);
  pop.remove();
  sel.classList.remove('select-search-open');
  document.removeEventListener('scroll', onScroll, true);
  window.removeEventListener('resize', onResize);
  document.removeEventListener('winnow:modalclose', onModalClose);
  if ((refocus || hadFocus) && sel.isConnected) sel.focus({ preventScroll: true });
}

export function selectSearchOpen() { return open ? open.sel : null; }

const onResize = () => closeSelectSearch();
const onModalClose = () => closeSelectSearch();
// A scroll anywhere but the list moves the select out from under a list
// that is fixed to where the select was.
function onScroll(e) {
  if (open && !open.pop.contains(e.target)) closeSelectSearch();
}

function paint(first) {
  const { input, list, note, entries } = open;
  const terms = queryTerms(input.value);
  const hits = terms.length ? entries.filter((e) => terms.every((t) => e.hay.includes(t))) : entries;
  const shown = [];
  const nodes = [];
  let group;
  for (const entry of hits.slice(0, RENDER_CAP)) {
    if (entry.group !== group) {
      group = entry.group;
      if (group != null) nodes.push(el('div', 'menu-header select-search-group', group));
    }
    const i = shown.length;
    const b = el('button', 'menu-item select-search-option');
    b.type = 'button';
    b.tabIndex = -1;
    b.id = `${list.id}-${i}`;
    b.title = entry.label;
    b.append(...marked(entry.label, terms));
    b.disabled = entry.disabled;
    b.setAttribute('role', 'option');
    b.setAttribute('aria-selected', String(entry.option.selected));
    if (entry.option.selected) b.classList.add('current');
    b.onclick = () => choose(entry);
    shown.push({ entry, node: b });
    nodes.push(b);
  }
  list.replaceChildren(...nodes);
  open.shown = shown;
  if (!hits.length) {
    note.textContent = `No option matches “${input.value.trim()}”.`;
    note.hidden = false;
  } else if (hits.length > shown.length) {
    note.textContent = `Showing the first ${shown.length.toLocaleString()} of ${hits.length.toLocaleString()} — type more to narrow the list.`;
    note.hidden = false;
  } else {
    note.hidden = true;
  }
  // Opening on the list as it is marks where the select stands; typing
  // marks the top hit, which is what Enter then takes.
  const current = shown.findIndex((s) => s.entry.option.selected && !s.entry.disabled);
  const firstOk = shown.findIndex((s) => !s.entry.disabled);
  setActive(!terms.length && current >= 0 ? current : firstOk, first);
}

function setActive(i, center = false) {
  const { shown, input, list } = open;
  if (open.active >= 0 && shown[open.active]) shown[open.active].node.classList.remove('active');
  open.active = i;
  if (i < 0 || !shown[i]) {
    input.removeAttribute('aria-activedescendant');
    return;
  }
  const node = shown[i].node;
  node.classList.add('active');
  input.setAttribute('aria-activedescendant', node.id);
  // By hand rather than scrollIntoView, which may scroll the page too.
  const top = node.offsetTop;
  const bottom = top + node.offsetHeight;
  if (center) list.scrollTop = Math.max(0, top - (list.clientHeight - node.offsetHeight) / 2);
  else if (top < list.scrollTop) list.scrollTop = top;
  else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight;
}

/* Step `by` places through the enabled options shown, wrapping at the
   ends the way the combo box's list does. */
function move(by) {
  const { shown } = open;
  const ok = shown.map((s, i) => (s.entry.disabled ? -1 : i)).filter((i) => i >= 0);
  if (!ok.length) return;
  const at = ok.indexOf(open.active);
  let next;
  if (at < 0) next = by > 0 ? 0 : ok.length - 1;
  else if (Math.abs(by) === 1) next = (at + by + ok.length) % ok.length;
  else next = Math.max(0, Math.min(ok.length - 1, at + by));
  setActive(ok[next]);
}

function choose(entry) {
  if (!open || entry.disabled) return;
  const { sel } = open;
  // The options may have been repainted while the list was up (a saved
  // format added); an option no longer in the select is not a choice.
  const index = [...sel.options].indexOf(entry.option);
  closeSelectSearch({ refocus: true });
  if (index < 0 || !sel.isConnected || sel.selectedIndex === index) return;
  sel.selectedIndex = index;
  sel.dispatchEvent(new Event('input', { bubbles: true }));
  sel.dispatchEvent(new Event('change', { bubbles: true }));
}

/* Under the select, as wide as it at least, flipping above when there is
   more room there. The height is fixed when it opens upward: its bottom
   edge is the one held against the select, and a list that shrank as the
   analyst typed would pull the search box down under the caret. */
function place() {
  const { sel, pop } = open;
  const r = sel.getBoundingClientRect();
  const m = 8;
  const gap = 2;
  pop.style.minWidth = `${Math.round(r.width)}px`;
  pop.style.maxHeight = 'none';
  const natural = pop.offsetHeight;
  const below = window.innerHeight - r.bottom - gap - m;
  const above = r.top - gap - m;
  const want = Math.min(MAX_HEIGHT, natural);
  const down = below >= want || below >= above;
  const room = Math.max(120, Math.min(MAX_HEIGHT, down ? below : above));
  pop.style.maxHeight = `${room}px`;
  if (down) {
    pop.style.top = `${Math.round(r.bottom + gap)}px`;
    pop.style.bottom = 'auto';
  } else {
    pop.style.height = `${Math.min(want, room)}px`;
    pop.style.top = 'auto';
    pop.style.bottom = `${Math.round(window.innerHeight - r.top + gap)}px`;
  }
  const w = pop.offsetWidth;
  let left = r.left;
  if (left + w > window.innerWidth - m) left = window.innerWidth - m - w;
  pop.style.left = `${Math.round(Math.max(m, left))}px`;
}

function onMouseDown(e) {
  const t = e.target;
  if (open && open.pop.contains(t)) {
    // A press on an option or a heading keeps the caret in the search box
    // (the click still lands). Not on the list itself: that is where its
    // scrollbar is, and a scrollbar has to stay draggable.
    if (t !== open.input && t.closest('.select-search-option, .menu-header, .select-search-note')) e.preventDefault();
    // A menu or panel holding the select must not read this as a press
    // outside itself and close, taking the select with it.
    e.stopPropagation();
    return;
  }
  if (open && t === open.sel) {
    // A second press on the select closes its list, as it does natively.
    e.preventDefault();
    closeSelectSearch({ refocus: true });
    return;
  }
  if (open) closeSelectSearch();
  if (e.button !== 0 || !isSearchable(t)) return;
  e.preventDefault();   // the browser's own list
  openSelectSearch(t);
}

function opensList(e) {
  if (e.key === 'F4' && !e.altKey && !e.ctrlKey && !e.metaKey) return true;
  if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && e.altKey) return true;
  // macOS opens the native list on a bare arrow, so this one does too.
  if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && IS_MAC && !e.ctrlKey && !e.metaKey) return true;
  return e.key === ' ' && !e.ctrlKey && !e.metaKey && !e.altKey;
}

const printable = (e) => e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;

function onKeyDown(e) {
  if (e.isComposing) return;
  if (open) {
    const inside = open.pop.contains(e.target) || e.target === open.sel;
    if (!inside) return;
    // The list owns the keyboard while it is up: nothing under it — the
    // dialog's Escape, the grid's keys, a panel's arrows — hears these.
    e.stopPropagation();
    switch (e.key) {
      case 'Escape':
        e.preventDefault();
        closeSelectSearch({ refocus: true });
        return;
      case 'ArrowDown':
      case 'ArrowUp':
        e.preventDefault();
        if (e.altKey) closeSelectSearch({ refocus: true });
        else move(e.key === 'ArrowDown' ? 1 : -1);
        return;
      case 'PageDown':
      case 'PageUp':
        e.preventDefault();
        move(e.key === 'PageDown' ? 10 : -10);
        return;
      case 'Enter': {
        e.preventDefault();
        const s = open.shown[open.active];
        if (s) choose(s.entry);
        return;
      }
      case 'Tab':
        // Back on the select, so the Tab carries on from there.
        closeSelectSearch({ refocus: true });
        return;
      default:
        // Typing with the focus off the box (after a press on the
        // scrollbar) still types into it.
        if (e.target !== open.input && (printable(e) || e.key === 'Backspace')) open.input.focus();
    }
    return;
  }
  const t = e.target;
  if (!t || t.tagName !== 'SELECT' || !isSearchable(t)) return;
  const starts = printable(e) && e.key !== ' ';
  if (!starts && !opensList(e)) return;
  e.preventDefault();
  e.stopPropagation();
  openSelectSearch(t, starts ? e.key : '');
}

/* DOM wiring, called once by main.js. Window and capture phase, for the
   reasons at the top of this file. */
export function wireSelectSearch() {
  window.addEventListener('mousedown', onMouseDown, true);
  window.addEventListener('keydown', onKeyDown, true);
}
