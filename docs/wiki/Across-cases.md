# Across cases

An intrusion usually spans several hosts, and a Winnow case usually holds one. **Across cases**, at the top right beside **Search all**, asks three questions of every case on this machine's case list at once, without opening them:

- **Where else**: which cases hold these values, and where.
- **Findings**: every case's tagged rows on one timeline.
- **Query**: one read-only SQL query over several cases.

Every answer is a list of lines that read like the [Timeline](Timeline-search-and-watchlist.md#the-timeline)'s, each with the case it comes from, and every line leads somewhere: you can tag it in its own case, or open it there.

## Choosing the cases

**Cases**, at the bottom of the list on the left, lists the cases on the home screen's case list. Every case is chosen to begin with; the checkboxes, **All** and **None** change that, and the line under **Cases** says how many are chosen (**All 4**, **2 of 4**).

- The case you have open is marked *open here*. It is read through the open case itself, so its answers include the tags you put on a moment ago.
- A case whose file is missing can't be chosen.
- A case another Winnow has open is marked *open in another Winnow* and names who has it. It is read like any other case, but its rows can't be tagged from here.

The other cases are read without being opened: Winnow doesn't take them over or change them, and leaves no files beside them. The only exception is a tag you put on a row yourself (see [Tagging a line in its case](#tagging-a-line-in-its-case)).

## Where else

Paste values into the box, one per line: hashes, IP addresses, accounts, file names. Leave it blank to use the open case's [watchlist](Timeline-search-and-watchlist.md#the-watchlist). Press **Search the cases**, or `Ctrl+Enter` (`⌘⏎`) in the box.

Each chosen case is searched the way [Search all](Timeline-search-and-watchlist.md#search-all) searches the open case: a row matches when a value appears anywhere in the columns its file came with, ignoring case for A–Z, and a table that has a search index in its own case is searched through it.

The results are grouped by case, then by table:

- Each case with hits gets a heading with its tables and hits (**WKSTN-022 1 table · 3 hits**).
- Each table gets a line with its type, its name, its match count, a count for each value that matched it, and **Open table ↦**.
- Under each table, its first 20 matching rows, as lines. After the body, each line names the values that row holds.
- When a table has more matches than that, *…and N more — open the table* follows the lines.
- Cases with no hits are named at the end (*No hits in SRV-FILE02.*). A case that can't be read is named at the top of the results with the reason, and the other cases still answer.

**Open table ↦** opens that case and table with the values in its Advanced search, so the grid shows every match. As with Search all, it first clears the table's filters, and a toast says it did.

A search runs in the background, so you can close the dialog and keep working. With the dialog closed, the button shows progress (*Across cases… 2/4*); when the search finishes, a toast counts the hits, and the button reads **Across cases (*N*)** until another search starts. **Stop** ends a search, and cases already read keep their results. Starting another search replaces the one running.

## Findings

**Findings** puts every chosen case's tagged rows on one timeline, in time order, as each case's own Timeline reads them. Press **Build**. **From** and **To** narrow it to a stretch of time, in the formats the Timeline's [timeframe](Timeline-search-and-watchlist.md#the-timeframe-filter) takes; either can be left blank.

- The header counts the findings, and gives each case a chip with its count.
- Each line has the **Gap** column, measured against the line above it whatever case that came from, so the jump from one host to the next reads as the time it took.
- At most 2,000 lines are listed. When there are more, the header says how many there are in all (**2,000 findings of 3,412 from 4 cases**).

Tag a row anywhere, in this case or another, and it is on **Findings** the next time you press **Build**.

## Query

**Query** runs one read-only `SELECT` with the chosen cases attached as `c1`, `c2`, and so on, in the order of the case list; the line above the box says which is which (*c1 = WKSTN-014 · c2 = SRV-DC01*). A query can attach at most 8 cases. **Show schema** lists every table of every attached case, with its columns. Each case's tables are named as they are in its own SQL pane (`src_1`, `src_2`), so `c2.src_1` is the first table of the second case. Statements that would change a case are refused, as in the [SQL pane](SQL-pane.md).

When a query's rows name rows of a table, they come back as lines:

- a `rid` column, and a query that reads exactly one table (`SELECT rid, UserName FROM c3.src_1 WHERE …`), or
- columns named `case` (holding `c1`, `c2`, …), `source_id` and `rid`, for a query that reads several (`SELECT 'c1' AS "case", 1 AS source_id, rid FROM c1.src_1 WHERE … UNION ALL SELECT 'c2', 1, rid FROM c2.src_1 WHERE …`).

**As a table** shows the columns as the query returned them, and **As lines** goes back. Any other result, such as a count or a `GROUP BY`, is shown as a table.

## Reading a line

A line has the Timeline's columns, plus one for its case:

| Column | What it shows |
| --- | --- |
| The gutter | A stripe per tag on the row, in its case's tag colors (hover for the tag's name), and `✎` when the row has a note |
| Timestamp | The row's timestamp, written `YYYY-MM-DD HH:MM:SS` |
| Gap | **Findings** only: how long after the line above this one happened |
| Case | The case the row is in, in a color of its own for each case |
| Type | The type you set under the Timeline's **Configure tables…**, else the kind of table Winnow recognizes (**Event logs** for EvtxECmd output), else the table's name |
| Body | The row's one-line [summary](Timeline-search-and-watchlist.md#summary-lines) when Winnow knows the artefact, else the whole row |

What you set under **Configure tables…** is kept per header set on this machine, so it applies to the tables of every case, not only the one you set it in.

Click a line to read it in full under the line: the tags of its case as chips, **Open in *case* ↦**, and every column that has a value. Click the line again to close it.

### Tagging a line in its case

Click a tag chip under a line to put that tag on the row, or to take it off. The tag is written in the case the row is in, since that is where its tags live and where its own Timeline, grid and exports will find it:

- A row in the case you have open is tagged there, and the grid shows the tag straight away.
- A row in another case is tagged by opening that case just for the write and closing it again. The line shows its new tag straight away.
- A case another Winnow has open is never written from here. Its chips are greyed out and say who has it open; tag the row in that Winnow instead.

Tags put on from here are not on the [`Ctrl+Z` history](Tagging-and-notes.md#undo). Click the chip again to take a tag off.

### Opening a line in its case

**Open ↦**, at the end of a line under the pointer, or **Open in *case* ↦** under a line you clicked, opens that case in this Winnow and its table at the row, with the row selected. If another Winnow has the case open, you are asked first, as on the home screen. The dialog keeps its results while you work in the other case: press **Across cases** again to come back to them.
