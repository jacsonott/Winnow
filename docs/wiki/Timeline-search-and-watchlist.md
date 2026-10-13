# Timeline, Search all and the Watchlist

Three pages work across the whole case rather than one table. The **Timeline** reads your findings back: every tagged row from every table, in time order. **Search all** answers "where does this appear?" for a term or a pasted list of indicators, table by table. The **Watchlist** keeps a standing list of indicators that is checked against every table, including tables you import later, and can tag what it finds. All three cover closed tables as well as open ones.

## The Timeline

![The Timeline: tagged rows from two tables in one stream](../screenshots/timeline.png)

Open the **Timeline** tab (top right, beside SQL and Notes). It lists every tagged row in the case, from every table, oldest first; tag a row anywhere and it is here. The header says how many tagged rows there are. It is rebuilt each time you open the tab, so it always reflects the tags as they are now.

Each line has three columns:

- **Timestamp** — the row's time, always written `YYYY-MM-DD HH:MM:SS` whatever display format Settings uses, so tables with different date formats sort together. A derived datetime column is used as it is, keeping any fractions of a second. Rows with no usable timestamp come last and show `—`.
- **Type** — a dot per tag on the row (hover for the tag's name) and a badge naming the kind of table: the file name unless you set a type (see [Choosing the timestamp, type and body](#choosing-the-timestamp-type-and-body)). Hover the badge for the file name.
- **Body** — a one-line summary of the row when Winnow knows the artefact, otherwise the whole row with its columns joined by ` | `. Hover it for the whole row.

**Click a line** to open its table at that row on the grid page. A closed table is opened for you and gets its tab back.

### Narrowing by tag

The chips beside **Tags** in the header choose which tags are on the Timeline. Every tag starts switched on; click a chip to take that tag's rows off, and again to put them back. A row with several tags stays while any of its tags is on.

A tag created after the case was opened starts switched off on the Timeline, so its rows are missing until you click its chip (or reopen the case).

### Summary lines

For the artefact shapes Winnow knows, the Body reads as a lead saying what happened and a few details identifying who and where:

```
Failed logon  svc_backup · 10.0.0.5 · WKSTN-4471 · id 4625
```

The lead is the first of the shape's lead columns that has something in it on that row. The details drop blanks, anything that repeats the lead, and the column already shown as the timestamp; some carry a word in front (`id 4625`, `size 1024`). Leads are cut at 100 characters and each detail at 120. Press **raw** at the end of a line to see the whole row instead, and **summary** to go back.

A table gets a summary when it contains every column of one of the shapes below (in any order, ignoring case; derived columns don't count). When several shapes fit, the one with the most columns wins.

| Shape | Lead (first non-empty) | Details |
| --- | --- | --- |
| Event logs (EvtxECmd) | MapDescription, Provider | UserName, RemoteHost, Computer, EventId (`id`) |
| Amcache — file entries | FullPath, Name | ProductName, Version (`version`), Size (`size`), SHA1 (`sha1`) |
| Amcache — device containers | FriendlyName, ModelName, KeyName | Manufacturer, ModelNumber, PrimaryCategory |
| Amcache — device PnPs | BusReportedDescription, Description, Model, KeyName | Manufacturer, Class, DriverName, HWID |
| Amcache — driver binaries | DriverName, KeyName | DriverCompany, Product, DriverVersion (`version`), Service |
| Amcache — driver packages | Directory, KeyName | Provider, Class, Version (`version`), Inf |
| Amcache — program entries | Name, ProgramId | Publisher, Version (`version`), RootDirPath, Source (`source`) |
| Amcache — shortcuts | LnkName, KeyName | KeyName |
| Shimcache (AppCompatCache) | Path | Executed (`executed`), SourceFile |
| Prefetch (PECmd) | ExecutableName, SourceFilename | RunCount (`runs`), Size (`size`), Volume0Name |
| Prefetch timeline (PECmd) | ExecutableName | — |
| Recycle Bin (RBCmd) | FileName | FileSize (`size`), FileType, SourceName |
| Windows Timeline — activity (WxTCmd) | DisplayText, Executable | Executable, ActivityType, Duration (`duration`), DevicePlatform |
| Windows Timeline — operations (WxTCmd) | DisplayText, Description, Executable | Executable, ActivityType, OperationType, DevicePlatform |
| Windows Timeline — package IDs (WxTCmd) | Name | Platform, AdditionalInformation |
| Jump lists — automatic (JLECmd) | Path, AppIdDescription | AppIdDescription, Hostname, InteractionCount (`interactions`), Arguments |
| Jump lists — custom (JLECmd) | EntryName, LocalPath, TargetIDAbsolutePath | AppIdDescription, LocalPath, Arguments, MachineID |
| LNK files (LECmd) | LocalPath, NetworkPath, TargetIDAbsolutePath, SourceFile | Arguments, WorkingDirectory, MachineID, FileSize (`size`) |
| Shellbags (SBECmd) | AbsolutePath, Value, BagPath | ShellType, BagPath, Slot (`slot`) |
| NTFS $Boot (MFTECmd) | SourceFile | VolumeSerialNumber (`serial`), ClusterSize (`cluster`), TotalSectors (`sectors`) |
| NTFS $MFT (MFTECmd) | FileName | ParentPath, FileSize (`size`), EntryNumber (`entry`) |
| USN journal $J (MFTECmd) | UpdateReasons | Name, ParentPath, EntryNumber (`entry`) |
| NTFS $SDS (MFTECmd) | OwnerSid | GroupSid (`group`), Id (`id`), Control (`control`) |
| Registry (RECmd batch) | Description, ValueName, KeyPath | KeyPath, ValueName, ValueData |
| SRUM — app resource use (SrumECmd) | ExeInfo | UserName, ExeInfoDescription, Sid |
| SRUM — network usage (SrumECmd) | ExeInfo | UserName, ProfileName, BytesSent (`sent`), BytesReceived (`received`) |
| ESXi / Linux host logs | Message | Component, User, SourceIP (`from`), Log |

### Choosing the timestamp, type and body

By default each table is placed on the Timeline by its **first datetime column** — a column Winnow recognised as dates when it imported the file, or after those, a derived datetime column. A table with no datetime column of its own has its rows at the end with `—`; give it one with a [derived datetime column](Derived-columns.md) (a Unix epoch or FILETIME column, say) and the Timeline uses it from then on.

To change this, press **Configure tables…** in the Timeline header. Every table in the case has an entry (merged tables are not listed; their rows appear under their member tables):

| Field | What it sets |
| --- | --- |
| Type | The badge text, in place of the file name |
| Timestamp | **(first datetime column)**, or any datetime column of the table |
| Body columns | Click columns to put them in the Body, in the order you click them. None picked means the default: the summary, or every column |

Press **Save** on that table's entry. Picking body columns replaces the shipped summary with exactly the columns you picked.

A configuration is saved for the table's set of columns, on this machine, and is reused by every table with the same columns in this case and in later cases — configure EvtxECmd output once and every future Security log lands the same way. Three things to know:

- Saving does not redraw the Timeline behind the dialog. It applies the next time the Timeline is built: open the tab again, or click a tag chip.
- A saved configuration is matched against all of a table's columns, derived columns included, but the dialog saves it under the file's own columns. Once a table has a derived column, its saved configuration no longer applies and the Timeline uses the defaults for that table.
- The same configuration decides the time and text of each row in the Watchlist's [Latest hits](#latest-hits-and-each-indicators-hits) list.

## Search all

Search all counts where terms appear across the tables of the case, without changing any table's filters. Open it with **Search all** at the top right or the `s` key; the dialog is titled **Search tables**. A search runs in the background, so you can close the dialog and carry on working.

### Choosing what to search

The row at the top of the dialog sets the scope:

| Button | Searches |
| --- | --- |
| **Every table** | Every table in the case, open or closed |
| **This table** | The table open in the grid — all of its rows, not just the ones the current filters leave. The default when a table is open |
| **Choose…** | The tables you tick in the row of table chips below it |

The chips under the buttons show which tables are in scope. Clicking a chip switches to **Choose…** with that table added or taken out.

Scoping to a merged table searches its member tables, where its rows live. The results name the members, and the dialog says so: *Logons is a merged table: its 3 member tables were searched, and the results name them.*

### Entering terms

**Paste a list** (the default) takes one term per line, and a row matches if it contains any of them — the shape of an IOC list of hashes, hostnames or IPs. **Import terms from file…** reads a `.txt`, `.csv`, `.list` or `.ioc` file into the list, one term per line, skipping blank lines, lines starting with `#` and duplicates.

**Advanced (AND / OR / NOT)** builds a query from term chips instead, the same way the grid's Advanced search does (see [Filtering and searching](Filtering-and-searching.md)).

Nothing runs until you press **Search** or `Ctrl+Enter` (`⌘⏎`) in the list. Matching is a plain substring match over the columns the file came with — derived columns are not searched — and ignores case for A–Z, like the grid's Contains search. Terms are not regular expressions.

### Reading the results

Each table with a match gets a line with its match count and an **Open ↦** button. In **Paste a list** mode, each term that matched that table gets its own indented line with its own count and **Open ↦**, so you can see which indicators hit where. Tables and terms with no matches are left out. One row can match several terms, so the term counts need not add up to the table's.

| Limit | What you see |
| --- | --- |
| A count reaches 1,000 | `1,000+ matches`; open the table for the exact number |
| More than 250 pasted terms | Table totals only, no per-term lines |
| Advanced mode | Table totals only |

A table that could not be searched is named at the top with the reason, rather than counted as no match.

**Open ↦** opens that table (opening it if it was closed) and puts the terms into its Advanced search — just that term from a term line, every term from a table line. Because the count covered the whole table, it first clears that table's column filters, filter builder, tag filter and the timeframe, and a toast names what it cleared.

### While a search runs

With the dialog closed, the **Search all** button shows progress (*Search all… 3/12*, or *Searching… 1/1* for a narrower scope). When it finishes, a toast says how many tables matched and the button reads **Search all (*N*)** (**Search (*N*)** for a narrower scope) until you reopen it. **Stop** ends a search; tables already counted keep their results. Only one search runs at a time, so starting another while one is running asks first (**Stop it and search**).

### Adding the terms to the watchlist

**Add to watchlist** adds the current terms (except any marked NOT) to the [watchlist](#the-watchlist) as type *other* with no auto-tag, skipping values already on it, and starts a scan for the new ones. That scan reuses what this search already found, so tables the search covered are not read again.

## The Watchlist

The Watchlist is the case's standing list of indicators: hashes, IPs, domains, file names, any string. It is stored in the case file. Each indicator is checked against every table, new imports are checked as soon as they land, and an indicator can tag the rows it matches. Open it with the **Watchlist** tab.

### Adding indicators

- **One at a time**: type the value in *Add an indicator — hash, IP, domain, filename…*, pick a type (**other**, **hash**, **ip**, **domain** or **filename**) and optionally a tag under **no auto-tag**, then press **Add**. A value already on the list is refused.
- **From a file**: **Import…** reads a `.txt`, `.csv`, `.ioc` or `.list` file. The **Import indicators** dialog shows how many it found and the first few, and asks for a **Type** and an **Auto-tag** for the batch. A line of the form `value,kind` (for example `10.1.2.3,ip`) carries its own type. Blank lines, lines starting with `#` and values already on the list are skipped.
- **From another case**: **From a case…** lists the other cases on this machine's home screen that have indicators. **Import** copies them in, skipping duplicates; types and notes come across, auto-tags do not (they name the other case's tags).
- **From Search all**: **Add to watchlist** in the Search all dialog (above).
- **From a profile**: a profile can bring a starter watchlist with it (see [Profiles](Dashboards.md#profiles)).

The type sets the label and colour on the row (HASH, IP, DOMAIN, FILE, IOC); it does not change how the indicator matches. There is no editing an indicator: to change its type or auto-tag, remove it with **✕** and add it again.

### How matching works

An indicator matches a row when its value appears anywhere in the row's own columns, ignoring case for A–Z — the same match Search all uses. It is a substring match: `evil.com` also matches `notevil.com`. Derived columns are not checked.

Every table is scanned except merged tables, whose rows are scanned in their member tables, and a table that is still importing, which is scanned when its import finishes. A table saved from a view is not scanned when it is created; the next **Scan all** covers it.

### Scanning

Scans start by themselves when you add or import indicators (for those indicators) and when an import finishes (every indicator against the new tables). **Scan all** re-reads every table for every indicator. A scan runs in the background with a **Watchlist scan** row in the jobs panel showing progress and a **Cancel** button; closing that row with **✕** cancels the scan too. Only one scan runs at a time; starting another takes over whatever the running one had left. Each scan replaces an indicator's hits for the tables it read.

### Auto-tagging

An indicator with an auto-tag puts that tag on every row it matches, each time it is scanned. Three consequences:

- Auto-tags are not on the [`Ctrl+Z` history](Tagging-and-notes.md#undo).
- Taking the tag off a matched row lasts only until the next scan, which puts it back. To stop it, remove the indicator; the tags it already applied stay on the rows.
- Deleting the tag in the tag editor turns that indicator's auto-tag off.

If the grid is showing a table the scan tagged, it repaints with the new tags when the scan finishes.

### Reading the list

Each indicator shows its type, its value, any note it arrived with, and a count:

| Count | Meaning |
| --- | --- |
| A number | Rows it matched |
| `…` | A scan for it is running |
| `not scanned` | No scan has read it against any table yet — this is not "not present" |
| `clean` | Every table has been scanned and nothing matched |
| `3/12 tables` | No hits in the tables scanned so far; the rest have not been read for it |

The summary line reads *4 indicators · 3 with hits · 1,240 hits*. When some rows matched more than one indicator it says so: *1,102 rows flagged · 1,240 hits counted across indicators* — the row count is the number of findings.

Two indicators that flag exactly the same rows, or where one's rows are all inside the other's, get a ⧉ chip (*same rows as …*, *all its rows are also …'s*, *covers every row of …*). Click it to open **Overlapping indicators** and keep one with **Keep "…"**. The other is removed, and its auto-tag moves across if the one you keep has none. Winnow refuses a merge that would lose rows only the removed indicator found.

### Latest hits and each indicator's hits

The page opens on **Latest hits**: the newest 60 flagged rows across every indicator, one line per row, newest first. Each line names the indicators that matched it (click a name to show only that indicator's hits), the table, the row's text and its time — the time and text come from the [Timeline configuration](#choosing-the-timestamp-type-and-body) for that table. Rows from a table with no datetime column come last.

Click an indicator in the list for **Hits for "*value*"**: its hits grouped by table, each table with its exact count and folded or unfolded by clicking its heading. Each hit shows its line number (`row 400`), the column the value was found in with that cell, and the row on one line. Up to 200 hits per table are listed; past that, *…and N more — open the table* opens the table at its first hit. **← all indicators**, or clicking the selected indicator again, goes back to Latest hits.

Click any hit, in either view, to open its table at that row on the grid page, opening the table if it was closed.

### New hits

When hits arrive that you have not looked at, the **Watchlist** tab, and its row in the sidebar's Pages list, carry a count of them (shown as `99+` above 99); the collapsed **Pages ▾** button shows a dot instead. Opening the Watchlist tab marks them seen. What you have seen is stored in the case file, so the count travels with the case. If a scan finds hits while you are on another page, its row in the jobs panel turns into *Watchlist: N hits* with a per-table breakdown and an **Open watchlist** button, and stays until you close it.

### Exporting and removing

**Export…** downloads `watchlist.csv` with the columns `value`, `kind`, `note` and `hits`. It is a record, not an import file: **Import…** reads `value` or `value,kind` per line, so the export's header line would come in as an indicator called `value` and its extra columns would end up in the type. To copy a watchlist to another case, use **From a case…** there.

**✕** on an indicator removes it and its hits. Tags it applied stay on the rows.
