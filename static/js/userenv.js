/* Settings → Environment — WINNOW_* environment variables for this user,
   the place for a token a plugin needs. The server only ever reports
   names and where each came from; a value is typed once, sent, and never
   comes back (winnow/userenv.py). Loopback-only, so a remote viewer sees
   the panel explain itself rather than a list. */
import { api, el, post, toast } from './core.js';
import { confirmDialog } from './ui.js';

const PREFIX = 'WINNOW_';

function sourceText(v) {
  if (v.reserved) return 'Winnow setting — read-only here';
  if (v.shell && v.stored) return 'set outside Winnow — that value wins; change it there';
  if (v.shell) return 'set outside Winnow — this run only';
  if (v.stored && v.live) return 'saved · active';
  if (v.stored) return 'saved · not loaded in this run';
  return 'set this run';
}

export function buildEnvPanel(b) {
  b.append(el('p', null,
    'Environment variables for your user account, for tokens and passwords a plugin '
    + 'needs. They stay on this machine: never in the case file, never in Winnow’s '
    + 'settings, never shown again once saved. Plugins read them as req.env("WINNOW_…").'));
  const where = el('p', 'fb-help');
  const list = el('div', 'env-list');
  // Built once, outside the list, so a repaint never discards what is
  // being typed (the case-variables panel learned this the hard way).
  const add = el('div', 'env-row env-add');
  const nameWrap = el('span', 'env-name-wrap');
  const nameIn = el('input', 'env-name-in');
  nameIn.placeholder = 'NAME';
  nameIn.spellcheck = false;
  // Same normalisation the server applies (upper-case) plus the one it
  // refuses: anything not a letter, digit or _ becomes _.
  nameIn.oninput = () => { nameIn.value = nameIn.value.toUpperCase().replace(/[^A-Z0-9_]/g, '_'); };
  nameWrap.append(el('span', 'env-prefix', PREFIX), nameIn);
  const valIn = el('input', 'env-value-in');
  valIn.type = 'password';
  valIn.placeholder = 'value (kept secret)';
  valIn.autocomplete = 'off';
  const save = el('button', 'btn ghost', 'Save');
  add.append(nameWrap, valIn, save);
  b.append(where, list, add);

  function paint(info) {
    where.textContent = `Only ${info.prefix}* names can be set or read. Stored in ${info.location}.`;
    list.replaceChildren();
    if (!info.vars.length) list.append(el('div', 'note-status', 'Nothing set yet.'));
    for (const v of info.vars) {
      const row = el('div', 'env-row');
      const name = el('span', 'env-name', v.name);
      name.title = v.name;
      const src = el('span', 'env-src', sourceText(v));
      const del = el('button', 'btn ghost env-del', '✕');
      del.title = v.reserved ? 'Set on the command line, not here' : 'Remove';
      del.disabled = !!v.reserved;
      del.onclick = async () => {
        if (!(await confirmDialog(`Remove ${v.name}? Plugins that read it will stop working until it is set again.`,
          { danger: true, okLabel: 'Remove' }))) return;
        try { paint(await api(`/api/env/${encodeURIComponent(v.name)}`, { method: 'DELETE' })); toast(`${v.name} removed`); }
        catch (e) { toast('Could not remove: ' + e.message, 6000); }
      };
      row.append(name, src, del);
      list.append(row);
    }
  }
  async function refresh() {
    try { paint(await api('/api/env')); } catch (e) {
      list.replaceChildren(el('p', 'fb-help', e.status === 403
        ? 'Only available from the machine Winnow is running on.'
        : 'Could not read the environment: ' + e.message));
      where.textContent = '';
      add.hidden = true;
    }
  }
  const submit = async () => {
    const raw = nameIn.value.trim();
    const name = raw.startsWith(PREFIX) ? raw : PREFIX + raw;
    if (!raw) { toast('Name the variable'); return; }
    if (!valIn.value) { toast('Give it a value'); return; }
    try {
      paint(await post('/api/env', { name, value: valIn.value }));
      toast(`${name} saved`);
      nameIn.value = ''; valIn.value = '';
    } catch (e) { toast('Could not save: ' + e.message, 6000); }
  };
  save.onclick = submit;
  valIn.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } };
  refresh();
}
