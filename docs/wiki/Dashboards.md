# Dashboards

A dashboard is a board of small summaries of the case: a row count, the top
remote hosts, failed logons over time, how many watchlist hits have landed.
Every number is a way in. Click it and the table opens filtered to the rows
that number counted, so a board is where a triage pass starts rather than a
report you read at the end. A case can hold as many named boards as you
like, and a board you build once can be saved to reuse on the next case of
the same kind, either on its own (the Library) or together with the plugins
it needs (a profile). Two profiles ship ready to use: **KAPE triage** and
**ESXi / UAC triage**.

Boards live in the case file, so they travel with it when you hand the case
to someone else. Their last results are kept in the case file too, so a
board opens without re-running every query; see
[Cached numbers and Refresh](#cached-numbers-and-refresh).

## Where boards live

Boards are listed in the sidebar under **Dashboards**, below your tables and
pages. Each row shows the board's name and how many widgets it has.

| To | Do this |
| --- | --- |
| Open a board | Click its name |
| Rename it | Double-click its name in the sidebar, or the title at the top of the board |
| Delete it | `✕` on its row. The widgets go with it; the case data is untouched |
| Pin it as a page tab | `⇱` on its row, or drag the row onto the sidebar's **Pages** header. It then appears as a tab in the page strip |
| Unpin it | `⇲` on its row, or close its page tab. Closing a pinned board's tab unpins it; the board stays in the case |
| Link to it from Notes | The Notes page's link menu lists every board as **Dashboard: *name*** (see [Tagging and notes](Tagging-and-notes.md)) |

Under the boards, a **Library** heading lists boards kept on this machine
and boards a plugin offers. See [The Library](#the-library).

## Creating a board

Click **＋ New dashboard** at the bottom of the sidebar's Dashboards section.
Give it a name and pick where it starts from:

| Start from | What you get |
| --- | --- |
| **Blank** | An empty board. Add widgets with **＋ Add widget** |
| **Starter for *table*** | A first board read from the table you have open (offered only when a table that isn't a merge is open, and picked by default then) |
| **Shipped: KAPE triage (10 widgets)**, **Shipped: ESXi / UAC triage (17 widgets)** | A copy of a shipped profile's board, on its own. It does not turn on the profile's plugins or seed its watchlist; [apply the profile](#applying-a-profile) for that |
| **Library: *name*** | A copy of a board you saved to the Library |

The starter is built from the table's shape alone: a row count; if the table
has a datetime column, its activity window (first and last) and an events
over time chart, bucketed by hour when the whole table spans two days or
less and by day otherwise; and a top-values bar chart for up to four columns
that have between 2 and 12 distinct values. Every widget it makes opens its
rows when clicked, and can be edited or removed like any other.

## Working with a board

The bar across the top of a board carries:

- the board's name (double-click to rename);
- **As of *time* · *age***, the age of the *oldest* result on the board;
- how many widgets run every time it opens;
- **↻ Refresh all**, **＋ Add widget**, **Save to library…** and **Save as profile…**.

Each card has a `⠿` grip (drag it to reorder), a small age mark, `⤴` to open the rows behind it, and `✎` to edit it. Everything
else about a widget, including removing it, is in the editor `✎` opens.
Cards are laid out on a four-column grid; a widget's **Width** is 1 to 4
columns.

**Empty cards fold into one line.** A card that has nothing to show is
hidden, and a strip at the top of the board lists it instead with the
reason. The two you will see most:

- **no "*header set*" table in this case yet**: the widget reads a kind of
  export the case doesn't have (no RECmd batch, say). **Import one ▸** opens
  the import dialog.
- **the table is in the case; the query matched nothing**: the widget looked
  and found nothing, which is itself a finding.

Others say that the table the card read is no longer in the case, or that
the case has no watchlist entries or no tags yet.

**Show them** puts the folded cards back on the board so you can edit or
remove them; **Fold them back** collapses them again. A `stat` card is never
folded, because 0 is an answer.

## Cached numbers and Refresh

A board paints what each widget returned last time, with its age on the
card, and runs only the widgets marked **Run every time the dashboard
opens**. Everything else is re-run when you ask:

- **↻ Refresh all** re-runs every widget on the board. If some can't run,
  they keep their previous numbers and a toast says how many failed.
- **Run now** in a widget's editor re-runs just that one.

When rows have been imported or tagged, or the watchlist has changed, since
a number was worked out, its age mark turns to a warning and the bar adds
**the case has changed since**. The number is still shown; ↻ Refresh re-runs
it. The check is deliberately coarse: a widget that reads something else,
such as a derived column's values or case notes, can go out of date without
being marked.

Tick **Run every time the dashboard opens** only for cheap widgets whose
number has to be current the moment the board appears, such as a watchlist
or tag count. The editor shows how long the widget took on its last run to
help you decide.

## Adding widgets

### The widget editor

**＋ Add widget** (in the bar, or the card of that name at the end of the board)
opens the editor; `✎` on a card opens it for that widget. You pick what to
show and which table it comes from, and the editor writes the query.

| Field | What it does |
| --- | --- |
| **Title** | The card's heading. Filled in from the template until you type your own |
| **Data from** | `sql` (a query against the case), `watchlist`, `tags`, or `cells` (a signals card, below) |
| **What to show** | A template, below. **Blank — write my own SQL** opens the SQL for you to write |
| **Table** | One of this case's tables, or a portable entry such as **Event logs (EvtxECmd) — any case (portable)**; see [Portable tables](#portable-tables-placeholders) |
| **Column**, **Match**, **Value**, **Bucket** | Shown when the template needs them |
| **Advanced — the SQL** | The query the template wrote. You can change it |
| **Render as** | How the answer is drawn; see [Render kinds](#render-kinds) |
| **Sub-label (optional, for stat)** | A line under a stat's number |
| **Width** | 1 to 4 columns |
| **Run every time the dashboard opens** | See [Cached numbers and Refresh](#cached-numbers-and-refresh) |

**Preview** runs the draft without saving it. **Save widget** saves it.
**Run now** and **Remove widget…** appear when you are editing an existing
widget.

Editing the SQL by hand makes the widget yours: the template stops applying,
and so does the click-through to rows. A widget with its own SQL and no
click-through opens its query in the SQL pane instead (see
[From a widget to its rows](#from-a-widget-to-its-rows)).

Widget SQL is read-only, with the same rules as the [SQL pane](SQL-pane.md),
and the editor's column lists leave out derived columns.

### Templates

| What to show | Renders as | Asks for | Clicking it opens |
| --- | --- | --- | --- |
| **Row count** | stat | — | The whole table |
| **Count of rows matching a value** | stat | Column, Match (`equals` / `contains`), Value. An empty value counts rows where the column is empty | The matching rows |
| **Distinct count of a column** | stat | Column (empty values are not counted) | Rows where the column is not empty |
| **Top values of a column (bar chart)** | bar, 2 wide | Column. The ten commonest non-empty values | Click a bar: rows with that value |
| **Rarest values of a column (list)** | list, 2 wide | Column. The ten rarest non-empty values | Click a row: rows with that value |
| **Events over time (histogram)** | histogram, 2 wide | Column (only datetime columns are listed when the table has any), Bucket (`per day` / `per hour`). Rows whose timestamp can't be read are left out | Click a bar: that day or hour |
| **Activity window (first / last)** | kv, 2 wide | Column (as for the histogram) | The whole table |

### From what you're looking at

The grid can add a ready-made widget without the editor. These entries are
hidden until you turn on **Case ▾ → Case settings… → Dashboards → Dashboard
creator mode**. It is off by default, so menus stay short for people who
only read boards, and it is stored in the case file, so it is set per case.

| Where | Entry | Adds |
| --- | --- | --- |
| Right-click a column header | Under the **Add to dashboard** heading: **Top values of *column***, **Distinct count of *column***, and for a datetime column **Events over time by *column*** | The matching template, on that column |
| Right-click a cell | **Add to dashboard** → **Count of *column* = *value*** | A count of rows with that value |
| **Filters ▾** | **Add to dashboard: count of this view** | One number: the rows the grid shows right now. Clicking it reopens the table with exactly these header filters, search, filter-builder conditions, timeframe and tag filter |

If the case has one board, the widget goes straight onto it. With none, you
are asked to name the first one (**Overview** is suggested). With several,
you pick which. On a merged view the header and **Filters ▾** entries are
not shown and the cell entry is greyed out. A derived column gets no header
entries, and the cell entry is greyed out on one.

Widgets added this way read *this case's* table, so they won't find it in
another case. To build a board you mean to reuse, use the portable tables in
the editor instead.

### From the SQL pane

**To dashboard…** in the [SQL pane](SQL-pane.md)'s toolbar turns the query
in the editor into a widget: pick a board (or **＋ New dashboard…**), and the
widget editor opens on it with your query, titled after the query tab and
rendered as `kv`. Adjust the render kind and save.

## Widget reference

### Data sources

| Data from | Answers with |
| --- | --- |
| `sql` | Your query's result, up to its first 200 rows |
| `watchlist` | One row per indicator with its hit count; as a stat, the total of all hits |
| `tags` | One row per tag with how many rows carry it; as a stat, the total |
| `cells` | A signals card: several small questions, each with its own answer and click-through |

### Render kinds

| Render as | Draws | Expects |
| --- | --- | --- |
| `stat` | One big number | The last column of the first row (or the watchlist/tags total) |
| `kv` | Label and value pairs | Two columns: label, value |
| `chips` | Yes/no pills: ✓ when the value is above 0 or `true`, ✗ otherwise | Two columns: label, value |
| `list` | A ranked list, first 12 rows | Label first, count last |
| `bar` | A horizontal bar chart | Label first, count last |
| `histogram` | Bars over time | Bucket label first, count last |
| `signals` | A grid of labelled numbers, some optionally as yes/no pills, each opening its own rows | `cells` |

A signals card is how the shipped KAPE board fits ten numbers in one card
without losing any of their click-throughs. A cell whose table isn't in the
case shows `—` (or `?` for a pill) in its own place and leaves the others
alone. The editor shows a signals card's cells read-only: you can change its
title, width and run-every-time setting, but a new signals card comes from a
board or profile that already has one.

## From a widget to its rows

Clicking a number opens its table filtered to exactly the rows it counted:

- **stat, kv and chips**: click the card body, or `⤴`.
- **bar and list**: click a bar or a row to open the rows with that value;
  `⤴` opens the table with no value picked.
- **histogram**: click a bar to open that day or hour. On a datetime column
  this sets the timeframe filter; on a text column that merely holds dates,
  it filters on the bucket's prefix instead.
- **signals**: each cell and pill opens its own rows.

The table opens fresh: the header filters, search, tag filter and timeframe
you had on it are cleared first, because they would hide rows the widget
counted. If the timeframe filter was on, a toast says it has been turned
off. The conditions arrive in the filter builder, where you can change them
like any other filter (see [Filtering and searching](Filtering-and-searching.md)).

A widget with its own SQL and no click-through opens its query in the SQL
pane instead, reusing the same query tab on later clicks. A widget that
counts across several tables (an `{{all:…}}` widget, below) asks
**This widget counts across several tables — open which?** and opens the
one you pick, so you see that table's share of the number.

## Portable tables: placeholders

A widget built on one of this case's tables names it by its internal id,
which is different in every case. A placeholder names a *kind of export*
instead, and resolves to whichever table in the open case matches it, so a
board saved to the Library or a profile works on the next case. The editor's
**Table** list offers the common ones as **— any case (portable)**; in your
own SQL you can write any of these:

| Placeholder | Resolves to |
| --- | --- |
| `{{evtx}}` | Event logs (EvtxECmd) |
| `{{mft}}` | NTFS $MFT (MFTECmd) |
| `{{usn}}` | USN journal $J (MFTECmd) |
| `{{registry}}` | Registry (RECmd batch) |
| `{{amcache}}` | Amcache — program entries |
| `{{prefetch}}` | Prefetch (PECmd) |
| `{{header_set:Full name}}` | Any shipped header set by its full name |
| `{{all:…}}` | Every matching table at once, e.g. `{{all:header_set:ESXi / Linux host logs}}` or `{{all:evtx}}` |

A table matches when it has every column of that header set (extra columns
are fine). A plain placeholder uses the first matching table. `{{all:…}}`
counts across all of them, which is what a support bundle needs: it arrives
as dozens of log files sharing one layout, and rotated logs imported later
are picked up with no rewiring. The shipped ESXi / UAC board is written this
way.

Merged tables, tables that failed to import, and tables saved with
**Save this view as a table…** are left out of an `{{all:…}}` union: a saved
view is a copy of rows another table already contributes, and counting it
too would count those rows twice. For the same reason they are not offered
when a union widget asks which table to open.

## The Library

The Library is boards kept on this machine, outside any case, so they can
be added to any case one at a time.

- **Save to library…** on a board's bar saves a copy under the name you give
  (the board's name by default). Saving under a name the Library already has
  replaces that entry.
- The Library is listed in the sidebar under **Dashboards → Library**. Click
  a name, or `＋` beside it, to copy that board into the open case.
- `✕` removes a board from the Library. Cases that already have a copy keep
  it.

**Adding a Library board replaces a board of the same name in the case,
without asking.** Rename the case's board first if you want to keep both.

Some plugins offer a board of their own, listed in the same place with only
`＋` (to remove one, turn the plugin off). Turning the plugin on never puts
its board in a case; it is copied in only when you add it. If the case
already has a board of that name that you built or edited, you are asked
first (**Replace it** / **Keep mine**); adding it again over its own untouched
copy just refreshes it. The bundled **esxi_logs** plugin offers
**ESXi / Linux host overview** this way (see [Plugins](Plugins.md#esxi_logs)).

## Profiles

A profile is "how I work this kind of case": the plugins it needs, the
boards it opens with, a starter watchlist, and the values the case must
carry (case variables). Profiles are kept on this machine and applied to a
case.

### Saving a board as a profile

**Save as profile…** on a board's bar saves, under the name you give:

- the plugins switched on for this case;
- this board, which is created under the profile's name when the profile is
  applied;
- the case's variable *definitions* (names, descriptions, whether required),
  never their values.

It is a snapshot: later edits to the board don't change the profile. Saving
under the name of one of your existing profiles updates that profile.

### The profile manager

Open it with `p` (the default key), or **Settings → Profiles → Open
profiles…**. Profiles are
listed on the left, tagged **shipped**, **applied** (to this case) or
**update**; the right side shows the whole of the selected one: every plugin,
every board with each widget's render kind and whether it runs on every
open, the watchlist and the variables.

| Button | Does |
| --- | --- |
| **＋ New profile…** | Build one from scratch: pick plugins, boards (from the Library or the open case) and variables |
| **Import…** / **Export…** | Read or write one profile as a single `.json` file. An imported profile whose name is taken is saved with **(imported)** added |
| **Save current plugins…** | Save just the plugins switched on now, under a name |
| **Copy to edit** | Shipped profiles are read-only; this starts an editable copy that remembers where it came from |
| **Edit…**, `✕` | Edit or delete one of your own. Deleting a profile doesn't change cases it was applied to |
| **Apply to this case…** | Opens the apply sheet (needs an open case) |

When a shipped profile you copied has since been revised, your copy shows
one line saying so and **See what changed**. **Take the shipped version**
replaces your copy with it, and your edits to the copy are lost; **Keep
mine** leaves it alone.
Nothing is updated automatically.

### Applying a profile

**Apply to this case…** shows what will change, counted against the open
case, as four parts you can tick separately. Nothing happens until you press
**Apply**.

| Part | What applying it does |
| --- | --- |
| **Plugins** | Sets every installed plugin on or off for this case: the profile's plugins on, **every other plugin off**. It also pins them, so turning a plugin on or off "everywhere" later, while another case is open, doesn't change this case. Plugins the profile names that aren't installed here are listed |
| **Boards** | Creates the profile's board, or replaces a board with the same name. If that board isn't the profile's own untouched copy (you've edited it, or built one with that name), the sheet warns you first |
| **Watchlist** | Adds the profile's indicators that the case doesn't already have, without auto-tagging, and starts a scan of the case's tables |
| **Variables** | Adds the variable definitions. Values already set are kept; required ones still empty are asked for after applying |

You can also pick a profile in the **New case** dialog's **Profile** list.
Every part is then applied as soon as the case is created, and required
variables are asked for in the dialog itself.

Applying a profile again replaces its board with the profile's current
version, which is how a case picks up a revised profile. Cases are never
changed by a profile on their own.

### The shipped profiles

#### KAPE triage

Windows host triage from KAPE output: EvtxECmd event logs, plus a RECmd
batch if you have one. Applying it turns on **lateral_movement** (and every
other plugin off for this case), seeds five filename indicators (`rclone`,
`psexec`, `mimikatz`, `anydesk`, `cobaltstrike`) and declares one optional
variable, **Engagement name**. Its board, **KAPE triage**, has ten widgets:

| Widget | Shows |
| --- | --- |
| **Host** | Computer name, hostname, IP addresses, domain, Windows version and build, and whether it's a workstation, server or domain controller, from the RECmd batch |
| **Logging posture** | Pills for Sysmon service, Sysmon driver, and PowerShell script block, module and transcription logging (from the registry); counts of **Logs cleared (1102)** and **Audit policy changed (4719)** |
| **Coverage** | The Security log's oldest and newest events, span in days and event count, and the window across all channels |
| **Triage signals** | Computers seen, distinct accounts, remote logon peers, failed logons (4625), explicit credentials (4648), RDP sessions (21/24/25), PowerShell blocks (4104), process creates (4688), Defender alerts, Run / service entries |
| **Findings** | Watchlist hits and tagged findings. The one widget that runs every time the board opens |
| **Logon volume over time (4624/4625/4648)** | Histogram |
| **Top remote hosts (4624)** | Bar chart |
| **Rarest logon accounts (long tail)** | List |
| **Registry entries by category** | Bar chart, from the RECmd batch |
| **Most recent Defender alerts** | The latest eight |

Without a RECmd batch, **Host** and **Registry entries by category** fold
into the empty-cards line with **Import one ▸**, and the registry pills read
`?`.

#### ESXi / UAC triage

VMware ESXi support bundles and UAC Linux collections. Applying it turns on
**esxi_logs** (and every other plugin off for this case) and declares the
same optional **Engagement name**; it seeds no watchlist. Turn it on, then
import the bundle: Winnow expands the archive and the plugin parses every
log into one layout (see [Plugins](Plugins.md#esxi_logs)). Its board,
**ESXi / UAC triage**, has 17 widgets, all counting across every parsed log
at once with `{{all:header_set:ESXi / Linux host logs}}`:

Log lines · Log types · Activity window · SSH logins accepted · Failed SSH
logins · Top source IPs · sudo / su use · Shell commands · Top shell
commands · VM power operations · Snapshot operations · vCenter-initiated
actions · Account / lockdown / firewall changes · VIB install / remove ·
Web / API access (rhttpproxy) · Severity mix · Log volume over time.

When the bundle produced more than one log table, clicking a number asks
which one to open (see
[From a widget to its rows](#from-a-widget-to-its-rows)).

## Limits worth knowing

- **A merged table can't be a widget's table.** It isn't offered in the
  editor, the Add to dashboard entries don't work on a merged view, and a
  placeholder never resolves to one. SQL you write yourself can read a merge
  by the name the SQL pane gives it (`merge_N`), but clicking that widget
  opens its query in the SQL pane, not rows.
- **Saved views are left out of `{{all:…}}` unions** and of the table picker
  a union widget shows.
- **A widget built on this case's table doesn't travel.** Use the portable
  tables for boards you'll save to the Library or a profile.
- **Derived columns** aren't offered in the widget editor or the Add to
  dashboard menus.
- **Hand-editing a widget's SQL removes its click-through to rows.**
- **Adding a Library board replaces a same-named board** in the case without
  asking.
- **Applying a profile turns off every plugin it doesn't list**, for that
  case.
- **Cached numbers can be out of date.** Check the age on the card, and use
  ↻ Refresh all before quoting a number.
- A `sql` widget reads at most the first 200 rows of its query, and a `list`
  shows the first 12.
