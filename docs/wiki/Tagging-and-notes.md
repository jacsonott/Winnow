# Tagging and notes

Tags are how a filtered view becomes a findings list. You filter down to the rows that matter, press a digit, and those rows carry the tag everywhere in the case: in the grid, on the [Timeline](Timeline-search-and-watchlist.md#the-timeline), in exports and in session files. Notes are the words that go with them — a note on a single row, and a Markdown narrative for the whole case on the Notes page. Tags and notes live in the case file; the evidence files are never touched.

## The default tags

A new case starts with three tags:

| Tag | Key | Colour |
| --- | --- | --- |
| TA | `1` | red |
| Suspicious | `2` | orange |
| Benign | `3` | green |

They come from the default tag template, which you edit under **⚙ Settings → Default tags for new cases**: change a name, colour or key, add one with **+ tag** or remove one with **✕**. Each change is saved as you make it, the same way as in the [tag editor](#editing-tags). The template is kept per machine (`workspace/tags.json` in the Winnow folder) and seeds every case created after the change, however it is created. To bring an existing case up to date, open **Edit tags** in that case and press **Apply default template**: it adds every template tag the case doesn't already have, matching by name and ignoring case.

A case that has no tags at all when it is opened gets the template again, so deleting every tag does not leave a case permanently tagless.

## Editing tags

Open the tag editor with the **Edit tags** chip at the right-hand end of the tag ribbon, or with **Edit tags…** at the bottom of a row's **Tag** submenu. Each tag is one line: a colour swatch, its name, its key and **Delete**.

There is no Save button. A colour or a key is saved as soon as you pick it, and a name once you stop typing or leave the box. "Saved" shows briefly beside the line. If a change can't be saved, the line says "Not saved" and a message gives the reason.

- **Rename or recolour**: change the name or pick a colour. A tag needs a name: while the box is empty the line says "Needs a name" and nothing is saved, and leaving the box puts the last saved name back.
- **Create**: **Add tag** adds a tag called "New tag" with no key and selects its name, so you can type the new one straight over it. Then give it a key.
- **Delete**: asks *Delete "name" and remove it from every row?* and, if you agree, takes the tag off every row in every table of the case. This cannot be undone with `Ctrl+Z`, and any watchlist indicator that auto-tagged with it stops auto-tagging.

### Tag keys

The key box takes one character, but only the digits `1` to `9` work as tag keys. A letter is accepted and shown on the chip, and pressing it does nothing. Give each tag its own digit: if two tags share one, the key tags with the one higher in the list. Tags without a key are still available from the right-click menu.

Tag keys are not part of the rebindable keyboard map in Settings — they belong to the tags, so the same case behaves the same way on any machine.

## Tagging rows

| Key | What it does |
| --- | --- |
| `1`–`9` | Toggle that tag on the selection (see below) |
| `Shift`+`1`–`9` | Tag every row in the view; when every row already has the tag, take it off them all |
| `Ctrl+Z` (`⌘Z` on a Mac) | Undo the last tag change; repeat to keep stepping back |
| Right-click a row | **Tag this row** (or **Tag *N* selected rows**) submenu, and **Undo** |

What a digit key acts on depends on what is selected (selection is covered in [Working with tables](Working-with-tables.md)):

| Selection | Rows tagged |
| --- | --- |
| Nothing but the cursor | The cursor row |
| A cell range across several rows | Every row the range covers |
| Rows ticked in the gutter | The ticked rows |
| Every row (header checkbox or `Ctrl+A`) | The whole view, minus any rows you untick |

The key is a toggle, and the first row of the selection decides the direction: if that row already has the tag, the key removes it from every selected row; otherwise it adds it to every selected row. Anything of 10,000 rows or more asks first (*Tag 12,000 selected rows as "TA"?*). A toast confirms what happened, for example *Tagged 4 rows · Suspicious*.

The keys work on the grid page. In the [SQL pane](SQL-pane.md) they tag the selected result rows when the query returns rows of a real table.

**From the right-click menu.** Right-click a row and open **Tag this row**: each tag is listed with its key, and a ✓ against the tags the clicked row carries. Clicking a tag toggles it, and the menu stays open so you can apply several in a row. Right-clicking inside a selection acts on the whole selection; right-clicking a row outside it acts on that row alone and leaves the selection as it was. Drag a tag (or click its ☆) to pin it to the top level of the menu, where it stays in this browser.

### Tagging a whole view

`Shift` plus a tag's digit applies the tag to every row in the current view — every row the column filters, search and timeframe leave, not just the rows on screen. It always asks first and says what it is about to do:

- *Tag all 1,204 rows in this view as "TA"? 40 already are.* — **Tag them**
- *Every row in this view is tagged "TA". Remove it from all 1,204?* — **Remove the tag**

The direction is decided by the whole view, never by one row: anything still untagged means tag them all, and only a view where every row has the tag is untagged. So a second press undoes the first, and the shortcut never removes a tag from part of a view.

### Tagging a group

With the grid grouped (see [Working with tables](Working-with-tables.md)), right-click a group header. Under **Tag *N* rows** each tag applies to every row in the group, whether or not the group is expanded and at any level of nesting. **Remove a tag instead…** switches the list to removing; **Apply a tag instead…** switches back. Groups of 10,000 rows or more ask first.

**+ Tag** in the **Group by** strip adds a level that buckets rows by their tags, with an *(untagged)* bucket for what is left. A tag group's header menu has **Filter to *tag*** in place of a value filter.

### Merged and saved tables

On a [merged table](Working-with-tables.md) a tag goes on the row in the member table it came from, so it shows there too and the Timeline lists it under the member's name. A table saved from a view starts with no tags; tags you put on it are its own and appear on the Timeline alongside the original table's.

## Seeing what is tagged

### The tag ribbon

The ribbon in the toolbar has one chip per tag: its colour, name, key and a count. The count is for the current view — the rows your filters, search and timeframe leave — and hovering the chip shows both numbers, for example *12 tagged in this view · 340 in the table*. A tag with nothing tagged in the view shows no count.

### Row stripes and the rail

Each tagged row has a coloured stripe per tag in its gutter, beside the line number, and a ✎ when it carries a note.

The rail is the narrow strip down the right-hand edge of the grid. Every tagged row in the view puts a mark on it at its position in the whole view, so you can see where findings cluster in a million rows without scrolling to them. Hover a mark to see which tags it is and which rows it covers (*TA · rows 1,180–1,214 of 52,000*). Scrolling the mouse wheel over the rail scrolls the grid.

## Filtering by tag

Click a chip in the ribbon to show only rows with that tag; click it again to drop it.

| Chips pressed | Rows shown |
| --- | --- |
| One tag | Rows with that tag |
| Two or more tags | Rows with any of them |
| **Any tag** | Rows with at least one tag (picking a specific tag replaces it) |
| **No tags** | Rows with no tag — what is left to go through |
| **No tags** and a tag | Untagged rows plus rows with that tag |

The tag filter combines with the column filters, search and timeframe. **Clear filters** (or `c`) clears it along with everything else. The other filter tools are on [Filtering and searching](Filtering-and-searching.md).

## Undo

`Ctrl+Z` (`⌘Z`) undoes the last tag change, and each press steps one change further back. The right-click menu shows what the next undo will do, for example **Undo: Tag 160 rows · Suspicious**. With nothing left, a toast says *Nothing to undo*. Inside a text box `Ctrl+Z` undoes typing as usual.

Undo restores exactly the rows a change moved, not the rows you aimed it at. Tag 200 rows when 40 already carried the tag and the change records 160; undoing it takes the tag off those 160 and the 40 keep it. Removing a tag works the same way the other round. A press that changed nothing — tagging rows that were already tagged — is not recorded.

What it covers and what it does not:

- **Covered**: the digit keys, `Shift`+digit, the right-click menu, tagging a group and tagging rows in the SQL pane.
- **Not covered**: row notes, renaming or recolouring tags, and tags a [watchlist](Timeline-search-and-watchlist.md#auto-tagging) scan applies.
- **Cleared**: deleting a tag drops its entries from the history; removing a table drops the entries that touched it; loading a session or **Start a fresh pass…** clears the whole history (see [Sessions and export](Sessions-and-export.md#loading-a-session)).

The history goes 25 changes deep, and older changes are also dropped once the history holds 5,000,000 rows in total, so one change that touched millions of rows can push earlier ones out. It lasts only while Winnow has the case open: restarting Winnow or switching to another case starts it empty.

## Row notes

A row note is a sentence or two on one row: why it matters, what it led to.

1. Open the row in the detail pane — double-click it, or press `d`.
2. Type in the **Note** box at the bottom of the pane (*Why does this row matter?*). `n` puts the cursor there while the pane is open.

The note saves itself shortly after you stop typing, and **Saved** appears under the box. Clearing the box deletes the note. A row with a note shows ✎ in its gutter. Each row has one note; on a merged table it is written on the member table's row.

Row notes travel with tags in session files. `Ctrl+Z` does not undo them.

## The Notes page

The **Notes** tab is the case narrative: a Markdown scratchpad saved in the case file, for the story you will write the report from. It saves as you type (**Saving…**, then **Saved**). It travels with the case file; a session file carries row notes but not this page.

The editor is on the left and a live preview on the right. **Edit** and **Preview** show one side only; **Split** shows both, and the page always opens in Split. Drag the divider to change the split and double-click it to reset it; the position is remembered in this browser.

### Markdown it understands

| You type | You get |
| --- | --- |
| `# Title` to `#### Title` | Headings, four levels |
| `**bold**`, `*italic*` | Bold, italic |
| `` `code` `` | Inline code, shown literally |
| A line of ```` ``` ```` before and after | A code block |
| `- item` or `* item` | A bulleted list |
| `> text` | A quote |
| `\| a \| b \|` followed by `\|---\|---\|` | A table |
| `[text](https://example.com)` | A link that opens in a new window (`http` and `https` only) |
| `[text](winnow:table/3)` | A link into the case (below) |

Numbered lists and HTML show as the text you typed, and images are not displayed.

### Links into the case

A note can link to a table, a SQL query tab or a dashboard of the case:

| Syntax | Clicking it in the preview |
| --- | --- |
| `[label](winnow:table/ID)` | Opens that table |
| `[label](winnow:sql/ID)` | Switches to that SQL tab |
| `[label](winnow:dashboard/ID)` | Opens that dashboard |

You rarely type these. **Link ▾** lists every table, query and dashboard in the case (**Table: …**, **Query: …**, **Dashboard: …**) and inserts the link at the cursor, labelled with its name. The ID is the case's own number for the item, so a link pasted into another case's notes points at whatever has that number there, if anything. A link to a table that has since been removed says *That table is no longer in this case*. There is no link to a single row; for that, use a row note.

### Row notes on the Notes page

Under the editor, **Row notes (*N*)** lists every row note in the case, ordered by table and line, as *table · Line 412* and the note on one line (hover for all of it). Click one to open its table at that row; a closed table is opened for you. The list shows the first 500 and says so when there are more. Fold it away with its heading; that choice is remembered in this browser.
