# Settings and shortcuts

Settings is where Winnow's look, its keyboard, and its machine-level
preferences live: the skin and theme, every rebindable key, the default
tags for new cases, file associations, plugins and updates. This page walks
through each section in the order Settings shows them, then gives the full
default keyboard map.

## Opening Settings

- Inside a case: the **⚙** button at the right of the top bar, or `?`.
- On the home screen: **⚙ Settings**.

Every section starts collapsed except **Shut down**; click a heading to open
it, and open as many as you like at once. **Close**, `Esc`, or `?` again
closes the panel.

Settings is about this machine and this browser. Choices that belong to one
case (its timestamp format, its variables) are under **Case ▾ → Case
settings…** instead, and travel with the case file (see
[Case settings](#case-settings)). Where each setting is kept matters when you
open Winnow in another browser, another browser profile, or on another port
(a quick-look from a file association runs on its own port):

| Kept in | Settings |
| --- | --- |
| This browser, and copied to the machine for any new browser or port to start from | Skin, theme, accent color, row density, launch animation, Pages as a dropdown, Open new tables when an import finishes, Keep the timeframe applied when switching tables |
| This browser only (per browser profile and address) | Keyboard shortcuts, striped rows, autofit width limit, always-on filter row |
| This machine (the `workspace/` folder) | Remote session mode, the default timestamp format, default tags for new cases, saved filters, plugins, profiles, the Windows hidden-start option |
| Your user account on this machine | File associations, environment variables |

## Appearance

### Skins

The card at the top shows the skin in use. Click it, or **Change skin…**, to
open the **Skins** panel, where each skin applies as you click it so you can
compare them; **Done** takes you back to Settings.

| Skin | Description as shown |
| --- | --- |
| Harvest | Grain and chaff — wheat gold on deep green-black, parchment in light. The default. |
| Panel | The older look — cooler greys, amber accent. |
| Phosphor | Retro CRT terminal — glow, monospace chrome. |
| Blueprint | Bold borders, hard offset shadows. |
| Studio | Rounded, soft shadows, calm motion. |
| Jenna's theme | Timeline Explorer in its Office 2016 Black skin — flat, square, Segoe UI, amber on cool grey. |

Every skin has a dark and a light form. Jenna's theme is the only one that
also redraws the grid itself (ruled cells, a closed box) rather than just
recolouring it, and the only one without striped rows by default.

### Theme

**Dark**, **Light** or **Auto**. Auto follows your operating system's light
or dark setting and switches when it does. Dark is the default.

### Accent color

Six preset swatches and a custom colour picker. Until you pick one, each
skin uses its own accent, with a separate shade for its light theme. Once you
pick a colour yourself, it stays when you change skin; Settings has no button
to go back to the skin's own accent.

### Row density, striped rows and the autofit limit

- **Row density**: **Comfortable** (the default, 24-pixel rows) or
  **Compact** (20-pixel rows). Switching keeps the same row at the top of the
  grid.
- **Striped rows**: alternating row shading in the grid and the Timeline.
  It is remembered per skin, so turning it off in one skin and switching to
  another can bring it back; the note under the box names the skin it is
  currently about.
- **Autofit column width limit**: how wide fitting a column to its content
  (the `=` key, or double-clicking a column's right edge) may make it.
  Default 900 px, anything from 80 to 20,000, or **No limit**. A column whose
  header name needs more room still gets room for its name.

### Remote session (RDP)

**Remote session mode** is for running Winnow inside RDP, VNC or another
remote desktop: the mouse wheel scrolls the grid by whole rows, and
animations and hover effects stop, so the remote display has far less to
send. Winnow asks about this once on first run. It is a machine setting, so
it holds for every browser and port.

### Start Winnow hidden (Windows only)

**Start Winnow hidden when a file is opened (no console window — the server
log stays hidden too)** appears only on Windows. It controls what a
double-clicked file (through a [file association](#file-associations))
starts: with it on, which is the default, Winnow starts without a console
window, which also means there is no server log to read. Turn it off while
troubleshooting an association. The change applies to every registered type
at once.

### Launch animation

**Launch animation** is the winnowing animation Winnow opens with. Any key
or click skips it. It is on by default. On macOS and Linux, left at its
default, it is skipped when your system asks for reduced motion, and the box
shows unticked; tick it to play the animation anyway. On Windows it plays
even with **Accessibility → Visual effects → Animation effects** off, because
many managed and virtual machines turn that off as a performance setting
rather than a preference; untick the box if you don't want it. The note
under the box says what happened at the last launch and why.

### Pages as a dropdown

Off by default. On, the page tabs (SQL, Timeline, Notes, Watchlist and any
plugin tabs) collapse into one **Pages ▾** button, giving the table tabs the
width; while a page is showing, the button carries that page's name. The
pages and their counts then live in that button's menu.

### Always-on filter row

On by default: every column has a filter box under its header at all times,
as in Timeline Explorer. Off, a filter bar above the grid shows only the
filters you have set, as removable chips, and the **⌕** on a column header
opens that column's box. See [Filtering and searching](Filtering-and-searching.md).

## Imports

**Open new tables when an import finishes** is off by default: a finished
import adds its tabs and leaves you where you are. On, the first table of a
batch to finish opens, and the rest arrive without moving you again. Import
itself is covered in [Cases and importing](Cases-and-importing.md).

## Keyboard shortcuts

The list shows every rebindable action with its keys as chips.

- To add a key, press **+ key** on that action's row, then press the key or
  combination. Hold modifiers for a combination (`Ctrl+Shift+K`); a capital
  letter is Shift plus the letter (`G`). `Esc` cancels.
- To remove a key, press the **✕** on its chip. An action can be left with
  no key at all.
- A key already used by another action is refused, and Winnow says which.
  So are the tag digits `1`–`9`, `Esc`, `Ctrl+C`, `Ctrl+Z` and `Alt` with a
  digit, which belong to the fixed keys below.
- **Reset to defaults** puts every binding back.

Bindings are saved in the browser, so they apply to this browser profile at
this address. A Winnow opened in another profile, or on another port, starts
from the defaults.

### Default keys

These are all rebindable. They are listed in the order Settings shows them,
with the action names Settings uses.

| Action | Default keys |
| --- | --- |
| Move the cell cursor down | `ArrowDown`, `j` |
| Move the cell cursor up | `ArrowUp`, `k` |
| Move the cell cursor left | `ArrowLeft` |
| Move the cell cursor right | `ArrowRight` |
| Jump to the first row | `Ctrl+ArrowUp`, `Meta+ArrowUp` |
| Jump to the last row | `Ctrl+ArrowDown`, `Meta+ArrowDown` |
| Jump to the first column | `Ctrl+ArrowLeft`, `Meta+ArrowLeft` |
| Jump to the last column | `Ctrl+ArrowRight`, `Meta+ArrowRight` |
| Page down | `PageDown` |
| Page up | `PageUp` |
| Jump to first row | `g` |
| Jump to last row | `G` |
| Focus search box | `/`, `Ctrl+f` |
| Filter the column under the cursor | none |
| Focus note field | `n` |
| Open settings (keyboard shortcuts, filter syntax) | `?` |
| Reset all column widths to default | `0` |
| Autofit all column widths to content | `=` |
| Previous saved filter | `[`, `q` |
| Next saved filter | `]`, `w` |
| Open the Filter builder (guided AND/OR conditions) | `e` |
| Open the value picker for the selected cell's column | `v` |
| Filter by selected cell's value | `f` |
| Filter by selected cell's value, dropping every other filter | `F` |
| Clear all filters, search and tag filter | `c` |
| Select every row in the current view (same as the header checkbox) | `Ctrl+a` |
| Copy the unique values of the cursor's column — from the selected rows, else the whole view | `Ctrl+Shift+U`, `Meta+Shift+U` |
| Open Tables manager | `t` |
| Show/hide the table sidebar | `` ` `` |
| Open the table menu (columns, value dropdowns) — also right-click a tab | `C` |
| Search all tables | `s` |
| Open/close the detail pane | `d` |
| Drop all grouping, restore column order | `x` |
| Save this column order/visibility as the default for this header set | `L` |
| Toggle the timeframe filter on/off | `r` |
| Open the timeframe filter (set column/range) | `R` |
| Show/hide the histogram of when the rows in this view happened | `h` |
| Toggle grouping off/on (remembers the last grouping) | `X` |
| Open the current filter as a query in the SQL pane | `Q` |
| Jump to timestamp… (set the moment and column) | `J`, `a` |
| Jump again to the saved timestamp (works across tables) | `.` |
| Profiles — the plugins, boards, watchlist and variables a kind of case carries | `p` |

`Meta` is `⌘` on a Mac and the Windows key elsewhere. The two "first row"
actions differ in one way: add Shift to `Ctrl+ArrowUp` and it extends the
selection to the first row, while `g` always just jumps.

**Focus search box** also answers to `⌘+F`, and to `Ctrl+F` with Shift or
Caps Lock, for as long as `Ctrl+f` stays bound to it. Winnow takes that chord
from the browser because the browser's find-in-page can only see the rows
currently drawn on screen. Remove `Ctrl+f` from the action and the browser's
find comes back.

### Fixed keys

These are not in the rebindable list.

| Key | What it does |
| --- | --- |
| `Shift` + an arrow, `PageUp` or `PageDown` | Grow the cell selection, the way dragging does. |
| `Ctrl+Shift` + arrow (`⌘+Shift` on a Mac) | Grow it all the way to the first or last row or column. |
| `Space` | Pick every row the cell selection covers, or let them all go if they are already picked. With no multi-row selection, toggles the cursor row. |
| `Shift+Space` | Turn the cell selection into picked rows, for tagging. |
| `1` – `9` | Toggle the tag with that key on the selection. |
| `Shift+1` – `Shift+9` | Apply that tag to every row in the current view, or take it off them if they all have it. Asks first. |
| `Alt+1` – `Alt+0` | Switch tabs: `1` is the table you were last in, `2` to `0` the page tabs in strip order. |
| `Ctrl+Z` / `⌘+Z` | Undo the last tag applied or removed. Repeat to keep stepping back. |
| `Ctrl+C` / `⌘+C` | Copy the cell selection, or the picked rows, as tab-separated text, the way the grid displays them. Up to 20,000 rows. |
| `Ctrl+Shift+C` / `⌘+Shift+C` | The same, with a header row. |
| `Esc` | Close a dialog; leave a text box; otherwise clear the row picks and cell selection. |
| Right-click a row | Tag it, filter to or exclude that cell's value, copy. |
| Right-click a tab | That table's menu: columns, value dropdowns, layout. |

The SQL pane has its own uses for `Ctrl+C`, `Esc` and the tag digits on a
selected result; see [SQL pane](SQL-pane.md).

### Tag keys

Tag keys are not part of the keymap. Each tag carries its own key, set in
the tag editor: **Edit tags** at the end of the tag ribbon, or **Edit tags…**
in a row's right-click menu. Only the digits `1` to `9` work as tag keys, and
Settings will not bind those digits to anything else. Give each tag its own
digit. The key shows on the tag's chip in the ribbon. Tags and their keys
belong to the case; new cases start from
[Default tags for new cases](#default-tags-for-new-cases). Tagging itself is
covered in [Tagging and notes](Tagging-and-notes.md).

### When keys work

- Most keys act on the grid and work only while a table is showing. `?`,
  `t`, `s`, `p` and `` ` `` also work from the SQL, Timeline, Notes,
  Watchlist and plugin tabs. `Alt+1`–`Alt+0` and `Ctrl+F` work anywhere in
  a case; `Ctrl+F` switches to the table first.
- While you are typing in a box, keys type. `Esc`, `Alt+1`–`Alt+0` and
  `Ctrl+F` still work.
- While a dialog is open, keys go to the dialog, except that the key that
  opened it closes it again (`?` for Settings, `C` for the table menu, `e`
  for the Filter builder).
- On the home screen only `Esc` does anything.
- Letter keys follow Caps Lock: with it on, `g` arrives as `G`.

## Filter & search syntax

A quick reference to the filter-row syntax (`svchost`, `!svchost`, `=4624`,
`!=4624`, `^C:\Users`, `>1000`, `<1000`, `>=1000`, `<=1000`, `/regex/`, `""`,
`*`, `a|b|c`), the value picker, the search box modes and the timeframe
filter. The full explanation is in
[Filtering and searching](Filtering-and-searching.md).

**Keep the timeframe applied when switching tables** is on by default: one
timeframe for the whole case, applied to whichever table you open. Off, each
table keeps its own, and a table you have not set one on opens without one.
It is the same switch as **Keep applied when switching tables** in the
timeframe dialog.

## Timestamps

**Every case on this machine** sets how datetime columns are displayed by
default. The choices are **As stored**, `YYYY-MM-DD HH:MM:SS` (the default),
`YYYY-MM-DD HH:MM:SS.mmm`, `YYYY-MM-DD HH:MM:SS.ffffff`, `YYYY-MM-DD`,
`HH:MM:SS`, `MM/DD/YYYY HH:MM:SS` and `MM/DD/YYYY`.

This is display only: the stored and exported value is always the text the
file came with. A format picked on one column (right-click its header) beats
the case's setting, which beats this one. A case sets its own under
**Case ▾ → Case settings… → Timestamps**.

## Default tags for new cases

The tag set every new case starts with. Each row is a colour, a name, a
one-character key and **✕** to remove it; **+ tag** adds a row. Nothing is
kept until you press **Save template**. Winnow ships with three:

| Tag | Key | Colour |
| --- | --- | --- |
| TA | `1` | red |
| Suspicious | `2` | orange |
| Benign | `3` | green |

The template does not change a case that already has tags. To bring a case up to
date, open its tag editor and press **Apply default template**, which adds
any template tag the case does not have (matched by name).

## Saved filters

Saved filters cycle with `[` / `]` (or `q` / `w`) on any table whose columns
they match. This section has **Open saved filters…** to browse, apply,
rename, reorder and delete them, **Export filters…** to download them as a
JSON file, and **Import filters…** to merge in a file someone exported. See
[Filtering and searching](Filtering-and-searching.md).

## Plugins

A summary line ("0 on of 7 installed" on a fresh install, plus how many failed
to load, if any) and **Manage plugins…**, which opens the plugin manager:
turning plugins on and off for the machine or one case, what each one adds,
and installing new ones. See [Plugins](Plugins.md).

## Profiles

How many profiles there are, shipped and your own, and **Open profiles…**
(also `p`). A profile is a kind of case: the plugins it needs, the boards it
opens with, a starter watchlist and the values the case must carry. See
[Dashboards](Dashboards.md).

## File associations

This section puts Winnow in your system's **Open With** menu for the file
types it reads, so you can open an export in Winnow straight from the file
manager, and optionally makes Winnow the double-click default.
Registration is per user, needs no admin rights, and unticking a type removes
it again. It works on Windows and Linux; on macOS the section explains that
associations need an app bundle, which a tool run from source does not have.
It is only available from the machine Winnow is running on.

Each row is a type with a checkbox (registered or not) and one of two
things beside it:

| Type | Offered |
| --- | --- |
| `.db-winnow` (Winnow case) | **Make default** |
| `.csv`, `.tsv` | **Make default** |
| `.jsonl`, `.ndjson` | **Make default** |
| `.txt`, `.json` | Open With only |
| `.db`, `.sqlite`, `.sqlite3` (SQLite database) | Open With only |
| `.xlsx`, `.xlsm` (Excel workbooks) | Open With only |
| Extensions claimed by a plugin that is on | **Make default**, labelled with the plugin's name |

"Open With only" types have owners already (editors, database browsers,
Excel), so Winnow adds itself to their Open With menu but will not take over
their double-click. A type that is already Winnow's default shows
**Default ✓**.

### What "Make default" does

**Make default** registers the type if it is not already, then asks the
system to open it with Winnow on a double-click.

- **Windows**: Winnow writes the per-user default under
  `HKCU\Software\Classes`. If Windows already holds a choice of app for
  that type, made through its own dialogs, it keeps that choice and ignores
  Winnow's, and Winnow tells you so. The supported way through is the
  system dialog: right-click a file of that type, **Open with → Choose
  another app**, pick Winnow and tick **Always**.
- **Linux**: Winnow adds itself under `[Default Applications]` in
  `~/.config/mimeapps.list`, editing only its own lines.

**Not working? Set it manually** at the bottom of the section gives the
manual steps for your system and the exact command this install registers,
which you can point the system at yourself.

The registration records this install's location and the Python that was
running Winnow when you registered. If you move the Winnow folder, or start
using a different Python or virtual environment, untick and re-tick the
types.

### What a double-click does

A double-clicked **case file** opens as that case: in a Winnow that already
has it open, in an idle Winnow with no case open, or in a new Winnow. A
double-clicked **data file** goes into a temporary *quick-look* case. Files
selected and opened together land in the same quick-look; a Winnow that has
a real case open is never switched to another case, so a new Winnow starts on
its own port instead. A quick-look case says so in a banner, with
**Save as a case…**, **Add tables to an existing case…** and **Discard…**.
One you never save and never do any work in is deleted after seven days.

### Offers you may see

- After an import, Winnow offers once per type to add itself to the Open With
  menu for the types you just imported. Either answer is remembered.
- Once Winnow is registered for at least one type, it asks at launch about
  any type eligible for **Make default** that it has not asked about before,
  such as one an update or a newly enabled plugin adds: **Make Winnow the
  default**, **Open With only** or **Not now**. Any answer is final for those
  types.

To remove Winnow completely, untick every type before deleting the folder;
see [Uninstalling](Installation.md#uninstalling).

## Environment

Environment variables for your user account, for the tokens and passwords a
plugin needs. Only names starting `WINNOW_` can be set. A value is typed
once, saved, and never shown again; the list shows only names and where each
came from. Plugins read them with `req.env("WINNOW_…")`.

They are stored on Windows as ordinary user environment variables
(`HKCU\Environment`), and elsewhere in `~/.config/winnow/env`, readable only
by you. A variable already set outside Winnow wins over the stored one and
cannot be changed from here. Any of Winnow's own configuration variables
that are set show as read-only. Values never go into a case file, which is why the case's own
variables (under [Case settings](#case-settings)) are the wrong place for a
secret. This section is only available from the machine Winnow is running
on.

## Updates

Shows the installed version, with **Check for updates** and, when there is a
newer release, **Install update**. Nothing is checked until you press the
button. See [Updating](Installation.md#updating) for what an update keeps,
the command-line updater, airgapped updates and rollback.

## Shut down

**⏻ Shut down Winnow** stops the server, after asking and warning about any
import or other work still running. Everything already committed is saved
in the case file. Every window using this server stops working until you
start Winnow again. See [Stopping Winnow](Installation.md#stopping-winnow).

## Case settings

**Case ▾ → Case settings…** holds the settings stored in the open case file,
which go with it when you hand it to another analyst:

- **Timestamps**: this case's display format, or **Use the machine-wide
  default**.
- **Dashboards**: **Dashboard creator mode** puts **Add to dashboard** back
  into column-header menus, row menus and the **Filters ▾** button. Off, those
  menus stay short; boards are always available under Dashboards. See
  [Dashboards](Dashboards.md).
- **Variables**: named values the case carries for plugins and the people
  working it (an engagement name, a link to the scoping document). Never put
  a token or password here; use [Environment](#environment).
