# Winnow

A local web app for reading very large delimited files out of SQLite, with the
row-tagging workflow that Timeline Explorer gets right and generic SQLite GUIs
don't. FastAPI + SQLite on the back, vanilla JS virtualized grid on the front.
No cloud, no CDN, no build step — it runs on an airgapped analysis box.

## Run it

```bash
pip install fastapi "uvicorn[standard]" python-multipart
python server.py --case case.db --open timeline.csv
```

Opens http://127.0.0.1:8777. You can also skip `--open` and import from the UI.

| flag | meaning |
| --- | --- |
| `--case FILE` | SQLite case file, created if missing. Everything lives here. |
| `--open A.csv B.csv` | Ingest files at startup |
| `--no-fts` | Skip the full-text index. Roughly halves import time; search falls back to substring matching |
| `--port`, `--host` | Defaults 8777 / 127.0.0.1 |
| `--no-browser` | Don't auto-open a browser |

## Measured on this machine

1.2M rows × 10 columns (169 MB CSV):

| | |
| --- | --- |
| Import | 8.2 s (~147k rows/s) |
| FTS5 index build | 8.8 s |
| Filter + sort 171k matching rows | 0.6 s |
| Fetch a page 150,000 rows deep | 1 ms |
| Full-text search across all columns | 8 ms |
| Tag all 171k rows in a view | instant |

## How it's put together

**Source tables are read-only.** Each import becomes `src_<id>` with an explicit
`rid INTEGER PRIMARY KEY`. Tags, notes, column layouts and saved views live in
sidecar tables keyed by `(source_id, rid)`. Re-import the same file and your
work is still there. Nothing ever writes back to the CSV.

**Scrolling stays O(window).** Naive `LIMIT/OFFSET` on a filtered sort degrades
badly once you're a few hundred thousand rows deep, because SQLite has to walk
every skipped row. Instead, changing a filter or sort materialises the result
once into a temp-attached table of `(pos, rid)`:

```sql
CREATE TABLE v.view_7 AS
SELECT ROW_NUMBER() OVER (ORDER BY "Timestamp" COLLATE NOCASE ASC, rid) AS pos, rid
FROM src_1 WHERE "Process" LIKE '%powershell%';
```

The grid then pages with `WHERE pos BETWEEN ? AND ?`, and the row count comes
free. Views live in a temporary database SQLite deletes on disconnect, so the
case file stays clean.

**Search** uses an external-content FTS5 table over every column, tokenized to
keep `.`, `-`, `_`, `\`, `@` and `:` inside tokens so paths, GUIDs and account
names survive tokenization. Bare terms are quoted before they reach the FTS
parser; `AND` / `OR` / `NOT` / `prefix*` pass through.

## Using it

Filters are typed straight into the box under each column header:

| type | means |
| --- | --- |
| `svchost` | contains |
| `!svchost` | does not contain |
| `=4624` | exact |
| `^C:\Users` | starts with |
| `>1000` | greater than (numeric columns cast to REAL) |
| `/re/` | regular expression |
| `""` | empty |
| `*` | not empty |
| `a\|b\|c` | any of |

Keys: `↑↓`/`jk` move, `Shift+↑↓` extend selection, `PgUp`/`PgDn`, `g`/`G` for
top and bottom, `1`–`9` toggle a tag on the selection, `Shift+1`–`9` apply a tag
to **every** row in the current view, `/` search, `f` first filter, `n` note,
`?` help.

Click a column header to sort, `Shift`-click to add a secondary sort.

The narrow strip down the right edge of the grid is a rail showing where tagged
rows sit in the whole filtered view — so you can see clustering in a 200k-row
result without scrolling through it.

## Sessions

**Session → Save session file** writes a small JSON containing tag definitions,
every tag assignment, notes, column layout and saved views, plus a hash of the
source file. Load it against the same evidence on another machine and it warns
you if the hash doesn't match. Tags are remapped by name on import, so two
analysts who both invented a "Lateral movement" tag end up merged rather than
duplicated.

**Export** writes the current view — filters, sort and search applied — as CSV
with `Line`, `Tags` and `Note` columns prepended, or just the tagged rows.

## SQL pane

Opens a read-only connection to the case file. `src_1`, `src_2`… are your data;
`row_tags`, `row_notes`, `tag_defs` are the sidecars. So this works:

```sql
SELECT t.name, s.Process, count(*) n
FROM row_tags rt
JOIN tag_defs t ON t.id = rt.tag_id
JOIN src_1 s ON s.rid = rt.rid
GROUP BY 1, 2 ORDER BY n DESC;
```

## Known limits

- Import is single-threaded Python `csv`. Fine to ~200k rows/s; if you routinely
  handle 50M-row files, swapping the ingest path to DuckDB's `read_csv_auto` and
  copying into SQLite is the next move.
- One view is materialised per source at a time. Changing a filter drops the
  previous one.
- No column reordering by drag yet — order is stored in the layout, so the
  plumbing is there.
- Multi-file correlation (one merged timeline across sources) isn't built.
