/* Entry point: wire the chrome, then start the app.

   Every other module here is declarations only — no top-level side effects —
   which is what makes their evaluation order (and any import cycle between
   them) unable to change behaviour. The two things that DO care about order
   live here, in the order the old single-file app.js ran them in:

   - the per-module wire*() calls, which attach DOM handlers. Nothing can
     fire during load, so these are order-independent; they're grouped only
     because that's easier to read than interleaving them below.
   - the startup sequence, which is not. loadKeymap() must precede
     updateTimeRangeButton() (it reads S.keymap for the tooltip);
     loadPageTabPrefs() must precede renderPageTabs(); loadDetailPrefs()
     must precede applyDetailPrefs(). Keep them in this order.

   Loaded as <script type="module">, so it is deferred and strict by default.
*/

import * as connection from './connection.js';
import * as core from './core.js';
import * as state from './state.js';
import { maybeOfferDefaultPrompt } from './assoc.js';
import * as jobs from './jobs.js';
import * as tabhistory from './tabhistory.js';
import * as charts from './charts.js';
import * as stack from './stack.js';
import * as notes from './notes.js';
import * as watchlist from './watchlist.js';
import * as dashboard from './dashboard.js';
import * as filters from './filters.js';
import * as splash from './splash.js';
import * as sources from './sources.js';
import * as view from './view.js';
import * as columns from './columns.js';
import * as tsformat from './tsformat.js';
import * as derived from './derived.js';
import * as grid from './grid.js';
import * as grouping from './grouping.js';
import * as tags from './tags.js';
import * as detail from './detail.js';
import * as ui from './ui.js';
import * as filterbuilder from './filterbuilder.js';
import * as savedfilters from './savedfilters.js';
import * as timeframe from './timeframe.js';
import * as merge from './merge.js';
import * as importer from './importer.js';
import * as tables from './tables.js';
import * as plugins from './plugins.js';
import * as search from './search.js';
import * as session from './session.js';
import * as sql from './sql.js';
import * as timeline from './timeline.js';
import * as rowmenu from './rowmenu.js';
import * as keymap from './keymap.js';
import * as profilebuilder from './profilebuilder.js';
import * as settings from './settings.js';
import * as userenv from './userenv.js';
import * as home from './home.js';
import * as errlog from './errlog.js';
import { toast } from './core.js';
import { applyDetailPrefs, loadDetailPrefs, wireDetail } from './detail.js';
import { wireFilters } from './filters.js';
import { wireGrid } from './grid.js';
import { wireGrouping } from './grouping.js';
import { maybeOfferStorageDir, boot, wireHome } from './home.js';
import { wireFileDrop } from './importer.js';
import { loadKeymap, wireKeymap } from './keymap.js';
import { wirePlugins } from './plugins.js';
import { wireSearch } from './search.js';
import { initAppearance, maybeOfferRemoteMode, wireSettings } from './settings.js';
import { applyPageTabsSize, initSidebar, wireSidebarResize, loadPageTabPrefs, renderPageTabs, wireSources } from './sources.js';
import { wireSql } from './sql.js';
import { S } from './state.js';
import { updateTimeRangeButton } from './timeframe.js';
import { wireTimeline } from './timeline.js';
import { wireUi } from './ui.js';


/* A flat, live view of every module's exports, for the browser console and
   for tests/ui (which reaches into app state through page.evaluate and has
   no other way in once the globals are gone). Getters, not a spread: a
   spread would freeze the value of a rebindable export like ROW_H at boot.
   Collision-free by construction — these names all shared one scope until
   the file was split. Not an API; nothing in the app reads it. */
const NAMESPACES = { splash, core, connection, state, jobs, tabhistory, charts, stack, notes, watchlist, dashboard, filters, sources, view, columns, tsformat, derived, grid, grouping, tags, detail, ui, filterbuilder, savedfilters, timeframe, merge, importer, tables, plugins, search, session, sql, timeline, rowmenu, keymap, settings, profilebuilder, userenv, home, errlog };
window.__winnow = {};
for (const ns of Object.values(NAMESPACES)) {
  for (const key of Object.keys(ns)) {
    Object.defineProperty(window.__winnow, key, { get: () => ns[key], configurable: true });
  }
}

// Mouse thumb buttons walk the recent-tab history (tabhistory.js). On
// mouseup, where browsers fire their own history navigation from.
window.addEventListener('mouseup', tabhistory.onMouseNav);

notes.wireNotes();
watchlist.wireWatchlist();
dashboard.wireDashboard();
wireSources();
wireGrouping();
wireDetail();
wireUi();
wireSql();
wirePlugins();
wireTimeline();
wireGrid();
wireFilters();
wireSearch();
wireKeymap();
wireSettings();
wireHome();

S.pageTabPrefs = loadPageTabPrefs();

applyPageTabsSize();

renderPageTabs();

S.detailPrefs = loadDetailPrefs();

applyDetailPrefs();

S.keymap = loadKeymap();

updateTimeRangeButton();

initAppearance();

initSidebar();
wireSidebarResize();

wireFileDrop();

/* The splash and boot run TOGETHER, not in sequence. boot() renders the
   case list behind the overlay, so when the animation finishes it fades
   onto a screen that is already there — the transition is a reveal, not a
   second load. It also means a slow case list waits behind something worth
   looking at rather than an empty page. */
if (splash.splashEnabled(S.appearance)) {
  splash.runSplash();   // takes its colours from the live skin, not an argument
}
// Presence, plus the banner that says so when the server stops answering.
connection.wireConnection();

boot().catch((e) => toast('Could not start: ' + e.message, 8000));

// Surface server-side errors in the app (Case menu → Error log) with a dot
// on the Case button when new ones land — they used to go only to the
// terminal.
errlog.startLogBadgePoll();

maybeOfferRemoteMode().then(() => maybeOfferStorageDir()).then(() => maybeOfferDefaultPrompt());
