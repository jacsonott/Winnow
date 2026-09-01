/* Plugin bundles — "case types": named per-machine sets of plugins,
   applied to the open case in one shot. A triage bundle turns on lateral
   movement + system info; a BEC bundle the user-activity set. Managed
   from the M menu here, and offered as "Case type" when creating a case
   (home.js). */
import { $, api, el, post, toast } from './core.js';
import { loadPlugins } from './importer.js';
import { renderPageTabs } from './sources.js';
import { S } from './state.js';
import { confirmDialog, markModalAction, modal, promptDialog } from './ui.js';

export async function listBundles() {
  return api('/api/plugin_bundles');
}

export async function applyBundle(bundle) {
  const res = await post(`/api/plugin_bundles/${bundle.id}/apply`, {});
  await loadPlugins();
  renderPageTabs();
  const missing = res.missing || [];
  toast(`Applied "${res.applied}" — ${res.enabled.length} plugin${res.enabled.length === 1 ? '' : 's'} on`
    + (missing.length ? ` (${missing.length} in the bundle not installed here: ${missing.join(', ')})` : ''), 6000);
  return res;
}

export function openPluginBundlesModal() {
  markModalAction('openPluginBundles');
  modal('Plugin bundles', async (b) => {
    b.append(el('p', 'fb-help',
      'Profiles for a kind of work — plugins, an optional dashboard, and a starter watchlist. '
      + 'Shipped profiles (like KAPE triage) are read-only; applying one sets THIS case’s plugins, '
      + 'loads its dashboard, and seeds its indicators. Save your own with the button below.'));

    const list = el('div', 'session-list');
    b.append(list);

    let bundles = [];
    try {
      bundles = await listBundles();
    } catch (e) {
      list.append(el('div', 'note-status', 'Could not load bundles: ' + e.message));
      return;
    }

    const caseOpen = !$('app').hidden;

    function render() {
      list.replaceChildren();
      if (!bundles.length) {
        list.append(el('div', 'note-status',
          'No bundles yet — set up the plugins you want (Settings → Plugins), then save them as a bundle below.'));
      }
      for (const bd of bundles) {
        const row = el('div', 'session-row browse-row');
        const name = el('span', 'session-name', bd.name);
        if (bd.shipped) name.append(el('span', 'bundle-shipped', 'shipped'));
        const bits = [bd.plugins.join(', ') || (bd.shipped ? '' : '(empty)')];
        if (bd.dashboard && bd.dashboard.length) bits.push(`dashboard · ${bd.dashboard.length} widgets`);
        if (bd.watchlist && bd.watchlist.length) bits.push(`${bd.watchlist.length} IOCs`);
        const plugins = el('span', 'count', bd.shipped ? (bd.description || bits.filter(Boolean).join(' · ')) : bits.filter(Boolean).join(' · '));
        plugins.title = bits.filter(Boolean).join(' · ');
        plugins.style.cssText = 'flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
        const apply = el('button', 'btn', 'Apply to this case');
        apply.disabled = !caseOpen;
        apply.title = caseOpen
          ? `Set this case's plugins to exactly: ${bd.plugins.join(', ') || 'none'}`
          : 'Open a case first — a bundle applies per case';
        apply.onclick = async () => {
          try {
            await applyBundle(bd);
            $('modal').hidden = true;
          } catch (e) {
            toast('Could not apply: ' + e.message, 6000);
          }
        };
        row.append(name, plugins, apply);
        if (!bd.shipped) {
          const del = el('button', 'btn ghost', '✕');
          del.title = 'Delete this profile (cases it was applied to keep their plugins)';
          del.onclick = async () => {
            if (!(await confirmDialog(`Delete profile "${bd.name}"?`, { danger: true, okLabel: 'Delete' }))) return;
            await api(`/api/plugin_bundles/${bd.id}`, { method: 'DELETE' });
            bundles = bundles.filter((x) => x.id !== bd.id);
            render();
          };
          row.append(del);
        }
        list.append(row);
      }
    }
    render();

    const acts = el('div', 'row-actions');
    const save = el('button', 'btn ghost', 'Save current plugins as a bundle…');
    save.title = 'Snapshot the plugins currently enabled (for this case, if one is open) under a name';
    save.onclick = async () => {
      const name = await promptDialog('Bundle name (e.g. Triage, BEC):');
      if (!name || !name.trim()) return;
      let current;
      try {
        current = await api('/api/plugins');
      } catch (e) {
        toast('Could not read the plugin list: ' + e.message, 6000);
        return;
      }
      const enabled = (current.plugins || []).filter((p) => p.enabled).map((p) => p.fs_name);
      try {
        const rec = await post('/api/plugin_bundles', { name: name.trim(), plugins: enabled });
        const i = bundles.findIndex((x) => x.id === rec.id);
        if (i === -1) bundles.push(rec); else bundles[i] = rec;
        render();
      } catch (e) {
        toast('Could not save: ' + e.message, 6000);
      }
    };
    acts.append(save);
    b.append(acts);
  });
}
