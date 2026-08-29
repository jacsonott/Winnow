/* The Settings modal — appearance, keyboard shortcuts, timestamps, tags.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { autofitMaxWidth } from './columns.js';
import { $, AUTOFIT_MAX_W_DEFAULT, ROW_H, ROW_H_COMFORTABLE, ROW_H_COMPACT, api, el, post, setRowH, toast } from './core.js';
import { labeledRow } from './derived.js';
import { VALUE_FILTER_AUTO_MAX } from './filters.js';
import { headH, rScroll, render, spacerPx, vScroll } from './grid.js';
import { drawRail, rebuildGroupPrefix, renderGrouped } from './grouping.js';
import { ACTION_LABELS, defaultKeymap, findKeyConflict, keySpecFromEvent, saveKeymap } from './keymap.js';
import { buildPluginsPanel } from './plugins.js';
import { loadSavedFilters } from './savedfilters.js';
import { applyPageTabsSize } from './sources.js';
import { S, gridRowCount } from './state.js';
import { openSavedFiltersModal, updateFiltersButton } from './timeframe.js';
import { TS_FORMATS } from './tsformat.js';
import { markModalAction, confirmDialog, modal, promptDialog } from './ui.js';

 // reads S.keymap.toggleTimeRange for its tooltip — must come after the line above

/* ------------------------------------------------------------ appearance */

/* Persisted the same way S.keymap is — localStorage, not server-side. This
   is a browser-local UI preference (which look you like), not case data,
   so it doesn't belong in workspace/ (cross-case but still server-side
   bookkeeping) any more than the keymap does. index.html has a small
   blocking inline script that mirrors just enough of this (read
   localStorage, set data-style/data-theme/--accent) before first paint, so
   returning users don't see a flash of the default look; initAppearance()
   below re-applies the same values once app.js loads, which is harmless
   and idempotent. */
export const APPEARANCE_KEY = 'winnow.appearance';

export const STYLES = {
  panel:     { label: 'Panel',     desc: "Today's look.", defaultAccent: '#d2a04a', preview: ['#13161a', '#d2a04a'] },
  phosphor:  { label: 'Phosphor',  desc: 'Retro CRT terminal — glow, monospace chrome.', defaultAccent: '#39e881', preview: ['#060907', '#39e881'] },
  blueprint: { label: 'Blueprint', desc: 'Bold borders, hard offset shadows.', defaultAccent: '#ff6a1a', preview: ['#0c0d10', '#ff6a1a'] },
  studio:    { label: 'Studio',    desc: 'Rounded, soft shadows, calm motion.', defaultAccent: '#7c6cf6', preview: ['#111219', '#7c6cf6'] },
};

export const ACCENT_PRESETS = ['#d2a04a', '#39e881', '#ff6a1a', '#7c6cf6', '#4a90d9', '#d9534f'];

export function defaultAppearance() {
  return {
    style: 'panel', themeMode: 'dark', accent: STYLES.panel.defaultAccent, accentCustomized: false,
    density: 'comfortable', autofitMax: AUTOFIT_MAX_W_DEFAULT,
    remoteSession: false,
  };
}

export function loadAppearance() {
  try {
    return { ...defaultAppearance(), ...JSON.parse(localStorage.getItem(APPEARANCE_KEY) || '{}') };
  } catch { return defaultAppearance(); }
}

export function saveAppearance() { localStorage.setItem(APPEARANCE_KEY, JSON.stringify(S.appearance)); }

export function contrastFg(hex) {
  const c = hex.replace('#', '');
  const r = parseInt(c.substr(0, 2), 16), g = parseInt(c.substr(2, 2), 16), b = parseInt(c.substr(4, 2), 16);
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.6 ? '#14181d' : '#ffffff';
}

/* Remote session mode (RDP and friends): the remote display protocol
   encodes screen *changes*, so every continuously-running animation keeps
   its encoder busy even when nothing is happening, and a hover repaint on
   every mousemove does the same. The class gates the CSS kill-switch at
   the bottom of style.css; the row-quantized wheel scrolling half lives in
   grid.js (wireGrid), keyed off the same setting. */
export function paintRemote() {
  document.documentElement.classList.toggle('remote', !!S.appearance.remoteSession);
}

/* First run on this machine (no stored appearance yet): offer Remote
   session mode once. Winnow is routinely used inside RDP on lab boxes,
   where the default smooth scrolling feels terrible and nothing hints
   that a fix exists three menus deep. Asked exactly once per machine —
   the answer (either way) is remembered, and the setting stays in
   Settings → Appearance. */
export async function maybeOfferRemoteMode() {
  const SEEN = 'winnow.remotePrompt';
  try {
    if (localStorage.getItem(SEEN) === 'seen') return;
    if (localStorage.getItem(APPEARANCE_KEY)) {
      // Not a first run — this machine predates the prompt. Don't nag.
      localStorage.setItem(SEEN, 'seen');
      return;
    }
    localStorage.setItem(SEEN, 'seen');
  } catch { return; }
  const yes = await confirmDialog(
    'First run on this machine — are you using Winnow through RDP or another '
    + 'remote desktop? Remote session mode scrolls by whole rows and stops '
    + 'animations, which remote displays handle far better. '
    + '(Change any time in Settings → Appearance.)',
    { okLabel: 'Enable remote mode', cancelLabel: 'No, local session' });
  if (yes) {
    S.appearance.remoteSession = true;
    paintRemote();
    saveAppearance();
  }
}

export function resolveAutoTheme() {
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function paintTheme() {
  document.documentElement.setAttribute('data-theme', S.appearance.themeMode === 'auto' ? resolveAutoTheme() : S.appearance.themeMode);
}

export function paintAccent() {
  document.documentElement.style.setProperty('--accent', S.appearance.accent);
  document.documentElement.style.setProperty('--accent-fg', contrastFg(S.appearance.accent));
}

/* Each style has a signature accent (the color it showed in the design
   review) — switching styles applies it, so the "vibe" actually changes,
   right up until the analyst manually picks a color themselves. After
   that, accentCustomized sticks and style switches stop touching it —
   their choice, not ours, from then on. */
export function applyStyle(styleName) {
  S.appearance.style = styleName;
  document.documentElement.setAttribute('data-style', styleName);
  if (!S.appearance.accentCustomized) applyAccent(STYLES[styleName].defaultAccent, false);
  saveAppearance();
}

export function applyThemeMode(mode) {
  S.appearance.themeMode = mode;
  paintTheme();
  saveAppearance();
}

export function applyAccent(hex, fromUser = true) {
  S.appearance.accent = hex;
  if (fromUser) S.appearance.accentCustomized = true;
  paintAccent();
  saveAppearance();
}

/* Sets the module-level ROW_H (see top of file — every virtualized-grid
   position calculation reads it) and mirrors it into --row-h so the CSS
   .row height actually matches what the JS thinks it is; the two must never
   drift apart or scrolling math and painted rows disagree about where
   things are. Safe to call before any case is open — it just primes ROW_H
   and the CSS var, there's no grid to re-lay-out yet. */
export function paintDensity() {
  setRowH(S.appearance.density === 'compact' ? ROW_H_COMPACT : ROW_H_COMFORTABLE);
  document.documentElement.style.setProperty('--row-h', ROW_H + 'px');
}

/* Changing density mid-session means every already-fetched pixel position
   (spacerY height, scrollTop, the translateY render() applies) is stale —
   this recomputes them all against the new ROW_H, and re-anchors scroll on
   the row that was at the top rather than a raw pixel offset, so switching
   density doesn't fling the view to a random spot. */
export function applyDensity(density) {
  S.appearance.density = density;
  saveAppearance();
  const body = $('body');
  // Read against the outgoing ROW_H (paintDensity hasn't run yet), written
  // back against the new one — so the anchor stays a row, not a pixel offset,
  // across a change that moves every pixel position in the grid.
  const total = gridRowCount;
  const topRow = S.view ? Math.floor(vScroll(body, total(), headH()) / ROW_H) : 0;
  paintDensity();
  if (!S.view) return;
  if (S.groupByCols.length) {
    rebuildGroupPrefix();
    $('spacerY').style.height = spacerPx(S.groupTotalRows) + 'px';
  } else {
    $('spacerY').style.height = spacerPx(S.view.row_count) + 'px';
  }
  body.scrollTop = rScroll(body, total(), topRow * ROW_H, headH());
  S.groupByCols.length ? renderGrouped() : render();
  drawRail();
}

export function initAppearance() {
  S.appearance = loadAppearance();
  document.documentElement.setAttribute('data-style', S.appearance.style);
  paintTheme();
  paintAccent();
  paintDensity();
  paintRemote();
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (S.appearance.themeMode === 'auto') paintTheme();
    });
  }
}

/* One collapsible section of the Settings modal. Returns the node the
   section's own code appends into — sections are otherwise written exactly
   as they were when they all ran into the modal body directly, which is
   also what keeps a section that fills itself later (buildPluginsPanel's
   async listing) landing inside its own section rather than at the end of
   the modal.

   Collapsed on open, every time: Settings had grown to seven sections and
   ~900px of scroll, so the thing you came for was rarely the thing you
   could see. Expansion state is deliberately not remembered — "open where
   I left it" and "collapsed by default" are different promises, and this
   one is the asked-for one. Several can be open at once; opening one
   doesn't close another.

   The header is a real <button> rather than a styled h4 so it's tabbable,
   keyboard-activatable and announces its aria-expanded state without any
   extra wiring. */
export function settingsSection(parent, title, { open = false } = {}) {
  const wrap = el('div', 'settings-section');
  const head = el('button', 'settings-section-head');
  const caret = el('span', 'settings-section-caret', '▸');
  head.append(caret, el('span', 'settings-section-title', title));
  const body = el('div', 'settings-section-body');
  const paint = () => {
    head.setAttribute('aria-expanded', String(open));
    caret.textContent = open ? '▾' : '▸';
    body.hidden = !open;
  };
  head.onclick = () => { open = !open; paint(); };
  paint();
  wrap.append(head, body);
  parent.append(wrap);
  return body;
}

export function openSettings() {
  markModalAction('openSettings');
  modal('Settings', (b) => {
    const secLook = settingsSection(b, 'Appearance');
    secLook.append(el('p', null, 'Pick a look, then a theme, then (optionally) your own accent color. All three are saved on this machine.'));

    const styleGrid = el('div', 'appearance-styles');
    for (const [key, meta] of Object.entries(STYLES)) {
      const card = el('button', 'style-card');
      card.setAttribute('aria-pressed', String(S.appearance.style === key));
      const sw = el('div', 'style-swatch');
      sw.append(el('span', null, null), el('span', null, null));
      sw.children[0].style.background = meta.preview[0];
      sw.children[1].style.background = meta.preview[1];
      card.append(sw, el('span', 'style-name', meta.label), el('span', 'style-desc', meta.desc));
      card.onclick = () => {
        applyStyle(key);
        styleGrid.querySelectorAll('.style-card').forEach((c, i) => c.setAttribute('aria-pressed', String(Object.keys(STYLES)[i] === key)));
        accentGrid.querySelectorAll('.accent-swatch').forEach((sw2) => sw2.setAttribute('aria-pressed', String(sw2.dataset.accent.toLowerCase() === S.appearance.accent.toLowerCase())));
        customAccent.value = S.appearance.accent;
      };
      styleGrid.append(card);
    }
    secLook.append(styleGrid);

    secLook.append(el('div', 'settings-sub-label', 'Theme'));
    const themeSeg = el('div', 'segmented');
    for (const mode of ['dark', 'light', 'auto']) {
      const btn = el('button', null, mode[0].toUpperCase() + mode.slice(1));
      btn.setAttribute('aria-pressed', String(S.appearance.themeMode === mode));
      btn.onclick = () => {
        applyThemeMode(mode);
        themeSeg.querySelectorAll('button').forEach((b2) => b2.setAttribute('aria-pressed', String(b2.textContent.toLowerCase() === mode)));
      };
      themeSeg.append(btn);
    }
    secLook.append(themeSeg);

    secLook.append(el('div', 'settings-sub-label', 'Accent color'));
    const accentGrid = el('div', 'accent-picker');
    for (const hex of ACCENT_PRESETS) {
      const sw = el('button', 'accent-swatch');
      sw.dataset.accent = hex;
      sw.style.setProperty('--sw', hex);
      sw.setAttribute('aria-pressed', String(hex.toLowerCase() === S.appearance.accent.toLowerCase()));
      sw.onclick = () => {
        applyAccent(hex);
        accentGrid.querySelectorAll('.accent-swatch').forEach((sw2) => sw2.setAttribute('aria-pressed', String(sw2.dataset.accent.toLowerCase() === hex.toLowerCase())));
        customAccent.value = hex;
      };
      accentGrid.append(sw);
    }
    const customAccent = el('input');
    customAccent.type = 'color';
    customAccent.id = 'appearanceAccentCustom';
    customAccent.value = S.appearance.accent;
    customAccent.title = 'Custom accent color';
    customAccent.oninput = (e) => {
      applyAccent(e.target.value);
      accentGrid.querySelectorAll('.accent-swatch').forEach((sw2) => sw2.setAttribute('aria-pressed', String(sw2.dataset.accent.toLowerCase() === e.target.value.toLowerCase())));
    };
    accentGrid.append(customAccent);
    secLook.append(accentGrid);

    secLook.append(el('div', 'settings-sub-label', 'Row density'));
    const densitySeg = el('div', 'segmented');
    for (const [key, label] of [['comfortable', 'Comfortable'], ['compact', 'Compact']]) {
      const btn = el('button', null, label);
      btn.setAttribute('aria-pressed', String(S.appearance.density === key));
      btn.onclick = () => {
        applyDensity(key);
        densitySeg.querySelectorAll('button').forEach((b2, i) => b2.setAttribute('aria-pressed', String(['comfortable', 'compact'][i] === key)));
      };
      densitySeg.append(btn);
    }
    secLook.append(densitySeg);

    secLook.append(el('div', 'settings-sub-label', 'Autofit column width limit'));
    secLook.append(el('p', 'fb-help',
      'How wide fit-to-content (the ' + (S.keymap.autofitColumnWidths[0] || '=') + ' key, or double-clicking a column\u2019s '
      + 'right edge) may make one column. A column whose header name needs more than this still gets '
      + 'room for its name. Uncapped, a single base64 command line can make the grid enormously wide.'));
    const capRow = el('div', 'row-actions');
    const capInput = el('input');
    capInput.type = 'number';
    capInput.min = '80';
    capInput.max = '20000';
    capInput.step = '20';
    capInput.style.cssText = 'width:90px;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px 7px;font:inherit';
    capInput.value = String(autofitMaxWidth() || AUTOFIT_MAX_W_DEFAULT);
    capInput.disabled = !autofitMaxWidth();
    const commitCap = () => {
      const v = Math.max(80, Math.min(20000, Number(capInput.value) || AUTOFIT_MAX_W_DEFAULT));
      capInput.value = String(v);
      S.appearance.autofitMax = v;
      saveAppearance();
    };
    capInput.onchange = commitCap;
    const noCapLabel = el('label');
    noCapLabel.style.cssText = 'display:flex;align-items:center;gap:6px';
    const noCap = el('input');
    noCap.type = 'checkbox';
    noCap.checked = !autofitMaxWidth();
    noCap.onchange = () => {
      // 0 is the stored spelling of "no cap" — distinct from a missing key,
      // which loadAppearance fills in with the default.
      S.appearance.autofitMax = noCap.checked ? 0 : (Number(capInput.value) || AUTOFIT_MAX_W_DEFAULT);
      capInput.disabled = noCap.checked;
      saveAppearance();
    };
    noCapLabel.append(noCap, el('span', null, 'No limit'));
    capRow.append(capInput, el('span', 'count', 'px'), noCapLabel);
    secLook.append(capRow);

    secLook.append(el('div', 'settings-sub-label', 'Remote session (RDP)'));
    secLook.append(el('p', 'fb-help',
      'For running Winnow inside RDP/VNC: scrolls the grid by whole rows (one repaint per wheel '
      + 'notch, like native apps), and stops animations and hover repaints, so the remote display '
      + 'encoder ships small deltas instead of re-encoding the viewport. Saved on this machine.'));
    const remoteLabel = el('label');
    remoteLabel.style.cssText = 'display:flex;align-items:center;gap:6px';
    const remoteCb = el('input');
    remoteCb.type = 'checkbox';
    remoteCb.checked = !!S.appearance.remoteSession;
    remoteCb.onchange = () => {
      S.appearance.remoteSession = remoteCb.checked;
      paintRemote();
      saveAppearance();
    };
    remoteLabel.append(remoteCb, el('span', null, 'Remote session mode'));
    secLook.append(remoteLabel);

    const secKeys = settingsSection(b, 'Keyboard shortcuts');
    secKeys.append(el('p', null, 'Tag hotkeys (1–9) are set per-tag in Edit tags. Escape always clears the selection or closes a panel. '
      + '"+ key" waits for a full press — hold modifiers for a combination (e.g. Ctrl+Shift+K), or Shift+letter for a capital.'));
    const list = el('div', 'settings-keys');

    function renderList() {
      list.replaceChildren();
      for (const [action, keys] of Object.entries(S.keymap)) {
        const row = el('div', 'settings-key-row');
        row.append(el('span', 'settings-key-label', ACTION_LABELS[action] || action));
        const chips = el('div', 'settings-key-chips');
        keys.forEach((k, i) => {
          const chip = el('span', 'settings-key-chip');
          chip.append(el('kbd', null, k));
          const rm = el('button', 'btn ghost', '✕');
          rm.title = 'Remove this binding';
          rm.onclick = () => { keys.splice(i, 1); saveKeymap(); renderList(); };
          chip.append(rm);
          chips.append(chip);
        });
        const addBtn = el('button', 'btn ghost', '+ key');
        addBtn.onclick = () => {
          addBtn.textContent = 'Press a key…';
          addBtn.disabled = true;
          const done = () => {
            document.removeEventListener('keydown', capture, true);
            addBtn.disabled = false;
            addBtn.textContent = '+ key';
          };
          const capture = (ke) => {
            ke.preventDefault();
            ke.stopPropagation();
            if (ke.key === 'Escape') { done(); return; }
            const spec = keySpecFromEvent(ke);
            if (spec == null) {
              // A modifier on its own is the *start* of a combination, not
              // the binding — keep listening, showing what's held so far.
              // (This used to commit immediately, so pressing Ctrl for
              // Ctrl+K bound "Control" and combinations were impossible.)
              let mods = '';
              if (ke.ctrlKey) mods += 'Ctrl+';
              if (ke.altKey) mods += 'Alt+';
              if (ke.metaKey) mods += 'Meta+';
              if (ke.shiftKey) mods += 'Shift+';
              addBtn.textContent = mods ? mods + '…' : 'Press a key…';
              return;
            }
            done();
            const conflict = findKeyConflict(spec, action);
            if (conflict) { toast(`"${spec}" is already used by ${conflict}`, 4000); return; }
            keys.push(spec);
            saveKeymap();
            renderList();
          };
          document.addEventListener('keydown', capture, true);
        };
        chips.append(addBtn);
        row.append(chips);
        list.append(row);
      }
    }
    renderList();
    secKeys.append(list);

    const reset = el('button', 'btn ghost', 'Reset to defaults');
    reset.style.marginTop = '14px';
    reset.onclick = () => { S.keymap = defaultKeymap(); saveKeymap(); renderList(); };
    secKeys.append(reset);

    const fixedKeys = el('div', 'kv');
    fixedKeys.style.marginTop = '10px';
    fixedKeys.append(el('kbd', null, 'Shift + move keys'), el('span', null, 'Extend the selection'));
    fixedKeys.append(el('kbd', null, '1 – 9'), el('span', null, 'Toggle the tag with that hotkey on the selection'));
    fixedKeys.append(el('kbd', null, 'Shift + 1 – 9'), el('span', null, 'Apply that tag to every row in the current view'));
    fixedKeys.append(el('kbd', null, 'Alt + 1 – 0'), el('span', null, 'Switch tabs — 1 is the table you were last in, 2 – 0 the page tabs in strip order'));
    fixedKeys.append(el('kbd', null, 'Ctrl/⌘ + Z'), el('span', null, 'Undo the last tag applied or removed (repeat to keep stepping back)'));
    fixedKeys.append(el('kbd', null, 'Esc'), el('span', null, 'Clear selection, or close a panel'));
    fixedKeys.append(el('kbd', null, 'Right-click a row'), el('span', null, 'Tag it, filter to or exclude that cell’s value, copy'));
    fixedKeys.append(el('kbd', null, 'Right-click a tab'), el('span', null, 'That table’s menu — columns, value dropdowns, layout'));
    secKeys.append(fixedKeys);

    const secSyntax = settingsSection(b, 'Filter & search syntax');
    const filters = [
      ['svchost', 'contains'],
      ['!svchost', 'does not contain'],
      ['=4624', 'exact match'],
      ['^C:\\Users', 'starts with'],
      ['>1000', 'greater than (numeric columns)'],
      ['/regex/', 'regular expression'],
      ['""', 'empty'],
      ['*', 'not empty'],
      ['a|b|c', 'any of these values'],
    ];
    const f = el('div', 'kv');
    for (const [a, c] of filters) { f.append(el('kbd', null, a), el('span', null, c)); }
    secSyntax.append(el('p', null, 'Column filter row:'), f);
    secSyntax.append(el('p', null,
      'The ▾ on a filter box lists that column’s distinct values with counts — tick the ones to keep. '
      + `It appears automatically on tables under ${VALUE_FILTER_AUTO_MAX.toLocaleString()} rows (reading the values is a scan); `
      + 'the table menu (right-click a tab) turns it on or off per table or per column, and a row’s '
      + 'right-click menu can open it for any column.'));
    secSyntax.append(el('p', null,
      'Search box — Contains is always a true substring match; Regex is a full scan; Advanced supports '
      + 'multiple AND / OR / NOT terms and uses the FTS5 index when one was built at import.'));
    secSyntax.append(el('p', null,
      'The ⏱ Timeframe button pins a start/end range against one datetime column, or every datetime '
      + "column at once (catches a row via its Modified time even if its Created time was timestomped) — "
      + 'unlike the other filters, it stays applied when you clear filters, apply a saved filter, or switch tables.'));

    const secTs = settingsSection(b, 'Timestamps');
    secTs.append(el('p', null,
      'How datetime columns are displayed. This is presentation only — the stored and exported '
      + 'value is always the text the file came with. A format picked on an individual column '
      + '(right-click its header) beats the case setting, which beats the system-wide one.'));

    const tsSystemSel = el('select');
    for (const [key, label] of Object.entries(TS_FORMATS)) {
      const o = el('option', null, label);
      o.value = key;
      tsSystemSel.append(o);
    }
    tsSystemSel.value = S.appSettings.default_ts_format || 'iso';
    tsSystemSel.onchange = async () => {
      try {
        S.appSettings = await post('/api/settings/app', { default_ts_format: tsSystemSel.value });
        render();
        toast('Default timestamp format saved');
      } catch (e) {
        toast('Could not save: ' + e.message, 5000);
      }
    };
    secTs.append(labeledRow('Every case on this machine', tsSystemSel));

    const tsCaseSel = el('select');
    const inherit = el('option', null, 'Use the system-wide default');
    inherit.value = '';
    tsCaseSel.append(inherit);
    for (const [key, label] of Object.entries(TS_FORMATS)) {
      const o = el('option', null, label);
      o.value = key;
      tsCaseSel.append(o);
    }
    tsCaseSel.value = S.caseSettings.ts_format || '';
    tsCaseSel.disabled = !S.sources.length && !S.sourceId;
    tsCaseSel.onchange = async () => {
      try {
        S.caseSettings = await post('/api/case_settings', { ts_format: tsCaseSel.value });
        render();
        toast('Case timestamp format saved');
      } catch (e) {
        toast('Could not save: ' + e.message, 5000);
      }
    };
    secTs.append(labeledRow('This case', tsCaseSel));

    const secTags = settingsSection(b, 'Default tags for new cases');
    secTags.append(el('p', null,
      "Seeds a brand-new case's tag set when you create one from the home screen. Doesn't change tags in "
      + 'any case that already exists — use "Apply default template" in Edit tags for that.'));
    const dtList = el('div', 'settings-keys');
    let defaultTags = [];

    function renderDefaultTagRows() {
      dtList.replaceChildren();
      defaultTags.forEach((t, i) => {
        const row = el('div', 'row-actions');
        const color = el('input'); color.type = 'color'; color.value = t.color || '#8899aa';
        color.oninput = () => { t.color = color.value; };
        const name = el('input'); name.value = t.name || ''; name.placeholder = 'Tag name';
        name.style.cssText = 'flex:1;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px 7px;font:inherit';
        name.oninput = () => { t.name = name.value; };
        const key = el('input'); key.value = t.hotkey || ''; key.maxLength = 1;
        key.style.cssText = 'width:34px;text-align:center;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:4px;font-family:var(--mono)';
        key.oninput = () => { t.hotkey = key.value || null; };
        const rm = el('button', 'btn ghost', '✕');
        rm.onclick = () => { defaultTags.splice(i, 1); renderDefaultTagRows(); };
        row.append(color, name, key, rm);
        dtList.append(row);
      });
    }

    api('/api/settings/default_tags').then((t) => { defaultTags = t; renderDefaultTagRows(); }).catch(() => {});
    secTags.append(dtList);

    const dtActs = el('div', 'row-actions');
    const dtAdd = el('button', 'btn ghost', '+ tag');
    dtAdd.onclick = () => { defaultTags.push({ name: 'New tag', color: '#7f9bb5', hotkey: null }); renderDefaultTagRows(); };
    const dtSave = el('button', 'btn', 'Save template');
    dtSave.onclick = async () => {
      defaultTags = await post('/api/settings/default_tags', { tags: defaultTags });
      renderDefaultTagRows();
      toast('Default tag template saved');
    };
    dtActs.append(dtAdd, dtSave);
    secTags.append(dtActs);

    const secFilters = settingsSection(b, 'Saved filters');
    secFilters.append(el('p', null,
      `Cycle through filters saved for the current source's columns with `
      + `${S.keymap.cyclePrevFilter[0] || '['} / ${S.keymap.cycleNextFilter[0] || ']'}. `
      + `Browse, apply, rename, reorder and delete them — and nickname their header sets — from `
      + `Filters ▾ → Saved filters (also reachable from the Filter builder). Save one from the `
      + `builder's "Save filter…" button.`));
    const fActs = el('div', 'row-actions');
    const openBtn = el('button', 'btn', 'Open saved filters…');
    openBtn.onclick = () => openSavedFiltersModal();
    const exp = el('button', 'btn ghost', 'Export filters…');
    exp.onclick = () => { window.location = '/api/saved_filters/export'; };
    const impLabel = el('label', 'btn ghost', 'Import filters…');
    const impInput = el('input');
    impInput.type = 'file';
    impInput.accept = '.json';
    impInput.hidden = true;
    impInput.onchange = async () => {
      const fd = new FormData();
      fd.append('file', impInput.files[0]);
      fd.append('merge', 'true');
      const res = await api('/api/saved_filters/import', { method: 'POST', body: fd });
      await loadSavedFilters();
      updateFiltersButton();
      toast(`Imported ${res.added} filter${res.added === 1 ? '' : 's'}`);
    };
    impLabel.append(impInput);
    fActs.append(openBtn, exp, impLabel);
    secFilters.append(fActs);

    const secPlugins = settingsSection(b, 'Plugins');
    buildPluginsPanel(secPlugins);

    const secUpdates = settingsSection(b, 'Updates');
    buildUpdatesPanel(secUpdates);
  });
}

/* Settings → Updates. Shows what this install is, and checks for a newer
   release ONLY when the analyst presses the button — Winnow has no
   startup ping and no background poll, deliberately: the analysis box may
   be airgapped (CLAUDE.md), and a forensic tool that reaches out to the
   internet unasked is its own problem regardless.

   Updating replaces the files Winnow ships and nothing else — workspace/,
   installed plugins, sessions and case files are never touched (see
   updater.PROTECTED) — and the previous version is backed up first, so a
   bad update is one button (or `python update.py --rollback`) away from
   undone. */
export function buildUpdatesPanel(b) {
  const status = el('div', 'fb-help');
  const notes = el('pre', 'update-notes');
  notes.hidden = true;
  const acts = el('div', 'row-actions');
  const checkBtn = el('button', 'btn ghost', 'Check for updates');
  const installBtn = el('button', 'btn', 'Install update');
  installBtn.hidden = true;
  acts.append(checkBtn, installBtn);

  const version = el('div', 'fb-help', 'Version: …');
  api('/api/version')
    .then((r) => { version.textContent = `Winnow ${r.version}`; })
    .catch(() => { version.textContent = 'Version: unknown'; });

  b.append(version, acts, status, notes,
    el('div', 'fb-help',
      'Checking contacts GitHub — nothing is sent, and Winnow never checks on its own. '
      + 'Updating keeps your saved filters, tags, plugins, case list and case files. '
      + 'On a machine with no network, download the release elsewhere and run '
      + '"python update.py --from <file>.zip" in the Winnow folder.'));

  let latest = null;
  checkBtn.onclick = async () => {
    checkBtn.disabled = true;
    status.textContent = 'Checking…';
    notes.hidden = true;
    try {
      const info = await post('/api/updates/check', {});
      latest = info;
      if (!info.available) {
        status.textContent = `Winnow ${info.current} is up to date (latest release is ${info.latest}).`;
        installBtn.hidden = true;
        return;
      }
      status.textContent = `Winnow ${info.latest} is available — you have ${info.current}.`;
      if (info.notes) {
        notes.textContent = info.notes.split('\n').slice(0, 40).join('\n');
        notes.hidden = false;
      }
      installBtn.hidden = false;
    } catch (e) {
      // The offline case is the expected one on an analysis box, not an error
      // to feel bad about — the message from the server says what to do.
      status.textContent = e.message;
    } finally {
      checkBtn.disabled = false;
    }
  };

  installBtn.onclick = async () => {
    if (!latest) return;
    const ok = await confirmDialog(
      `Install Winnow ${latest.latest}?\n\n`
      + 'Your saved filters, tags, installed plugins, case list and case files are kept — '
      + 'only the program files are replaced, and the current version is backed up first.\n\n'
      + 'Winnow has to be restarted afterwards to run the new version.',
      { okLabel: `Install ${latest.latest}` });
    if (!ok) return;
    installBtn.disabled = true;
    status.textContent = 'Downloading and installing…';
    let res;
    try {
      res = await post('/api/updates/apply', { confirm: true });
    } catch (e) {
      status.textContent = 'Update failed: ' + e.message;
      installBtn.disabled = false;
      return;
    }
    installBtn.hidden = true;
    status.textContent = `Updated ${res.previous_version} → ${res.version}. Restart Winnow to run it.`;
    toast(`Updated to ${res.version} — restart Winnow to run the new version`, 12000);
  };
}

/* DOM wiring for this module, called once by main.js. Handlers can't
   fire during load, so the order these run in doesn't matter — the
   startup steps that DO depend on order live in main.js instead. */
export function wireSettings() {
window.addEventListener('resize', () => { render(); drawRail(); applyPageTabsSize(); });
}
