"""First/Last plugin — the bounds of each group of events, as a new table.

Group a table on any columns (Host + User, say), and get back two rows per
group: the first and the last event, ordered by a column you choose, each
carrying a templated description and whichever columns you want alongside.
The classic use is turning 40,000 logon events into 200 session bookends —
"First of 312 | WKSTN-014 | user: jsmith" — that read as a story and can be
tagged, put on the Timeline, and exported like any other rows.

Shape decisions worth knowing before editing:

**The result is a real table** (Store.ingest_rows), not a live view in the
tab. Bookends are findings: the analyst tags them, they belong on the
unified Timeline, and they should survive in the case file when it's handed
over — none of which a plugin tab's in-memory state can do. ingest_rows
gives every ingest convention (all-TEXT, contiguous rids, batched commits,
background FTS) for free, and invariant #1 holds untouched: the source
table is only ever read.

**First/last come from one grouping pass, then a lookup by rid.** A
group's first row is the one `ORDER BY sort, rid` puts first and its last
the one `ORDER BY sort DESC, rid DESC` puts first; `rid` tie-breaking
makes the answer deterministic when timestamps collide, which EVTX at
second resolution does constantly. _row_key spells (sort value, rid) as one
text that sorts the same way, so a plain GROUP BY's MIN and MAX of it name
both rows (_groups_sql), and only those rows' columns are read afterwards.
It used to be window functions — ROW_NUMBER() both ways and a COUNT() —
and SQLite sorts the whole filtered table once per window, carrying every
column the output needs through each sort: with the preview's separate
group count, 9.5 s on a 693k-row table that the same GROUP BY typed into
the SQL pane reads in 1.5 s, and nearly a minute with the JSON cell on. A
number-typed sort column is ordered by value, like the grid's numeric
sort, never as text ("10" < "9"); a cell that isn't a number wins neither
end (`_order_dir`). No text spells a REAL in order, so that case finds
each group's smallest and largest value first and their rows second.

**The description is rendered server-side, per row.** `{ColumnName}` takes
that row's value (so the first row shows the first event's user),
`{count}` the group's row count, `{which}` the literal First/Last, and
`{which:number}` the row's position in its group instead (1 for the first,
the group size for the last). Unknown
placeholders are a ValueError naming the offender — a template typo should
fail the preview, not silently emit `{Usre}` into 400 descriptions. Any
column of the source can be named: the bookend query reads whatever the
template references on top of the grouped/ordered/carried columns
(`_template_columns`), so a column need not be dragged into Include
columns just to appear in the description. The one exception is a column
named like one of the pass's own aliases (`_reserved_aliases`): that is
never projected on the template's behalf, so `{count}` and the Sum column
keep reading the group's numbers rather than the file's.

**Distinct values are a second, separate pass** (`_group_values`). A
column under List values, or named by `{values:Column}`, becomes each
group's distinct non-blank values, sorted, in one cell. It cannot ride the
bookend pass: group_concat(DISTINCT …) can neither sort its values nor stop
at MAX_LISTED_VALUES, so a column of ids grouped by host would gather every
id of the host only to cut the list down afterwards. So it is
one GROUP BY per column over the same filtered rows, keyed back onto the
bookends by their group values, and capped at MAX_LISTED_VALUES per cell
(the rest counted, not dropped silently). The preview asks only for the
groups it shows.

**Counts and totals ride the bookend pass** (`_groups_sql`). A unique
count needs neither an order nor a cap, so a column under Count unique, or
named by `{unique:Column}`, is one COUNT(DISTINCT …) in the GROUP BY that
already counts each group, blanks left out as List values leaves them out.
Totals work the same way: `{sum:}`, `{min:}` and `{max:}` take any number
column, under Total up or not; only Total up's sums become output columns.

**The tab asks for results as background jobs** (`job/start`, `job`,
`job/cancel`), never by holding a request open for the work. On a big table
the work is long — 15 s for a preview and 18 s for Create on 1M rows — and
a request held that long holds one of the six connections a browser opens
to Winnow, one of which is always the presence stream. The tab used to
wait on preview/rows/create directly, and an edit under Auto-update left
the preview before it running: a handful of edits held every connection,
and a scroll in another tab took 67 s to paint because the grid's page
fetch queued in the browser behind them. Now the work runs on a thread
here, start answers within JOB_START_WAIT_MS (with the result, when a
small table had it by then), the tab polls with short requests, and it
cancels the job an edit supersedes. A job's queries run under its id as
the op token, so a cancel interrupts the SQL itself (Store.cancel_op), and
the id is made by the client, like the app's op tokens, so a cancel sent
before start has answered still lands. The synchronous routes stay, for
scripts and tests; a job runs exactly their code.

The filter helpers (_where/_esc_like/OPERATORS) are copied from the pivot
example rather than imported — plugins are deliberately standalone
(plugin_api gives them no way to import each other), and each example is
meant to be readable alone.
"""

import json
import re
import sqlite3
import threading
import time
import traceback

from winnow.store import OpCancelled, q, union_all

PLUGIN = {
    "name": "first-last",
    "version": "1.0.0",
    "description": "Bookend each group of events: first and last row per group, with a templated description, as a new table.",
}

WINNOW_API_VERSION = 13   # winnow.dropdownMenu: the click-to-place menu under a field

MAX_GROUP_COLS = 6
# Two output rows per group; beyond this the result stops being a summary.
MAX_GROUPS = 100_000
PREVIEW_GROUPS = 8
# "Copy result" returns the whole result to the clipboard; past this it
# stops being something a clipboard (or the analyst) wants.
MAX_COPY_ROWS = 20_000
# A List values cell names at most this many values and counts the rest:
# past a few dozen a cell stops being read, and a column of ids grouped by
# host would otherwise put a whole column into every bookend.
MAX_LISTED_VALUES = 50
# The description used when the analyst leaves the box empty.
DEFAULT_TEMPLATE = "{which} of {count}"
# The auto-created timeline tag's colour — fixed so every bookend tag
# reads the same across cases (amber, matching the app's accent family).
TIMELINE_TAG_COLOR = "#d2a04a"
# How long job/start waits for its job before answering "running". A small
# table's result is back well inside it, so it comes back inline and the
# tab never polls; holding a connection this long costs nothing.
JOB_START_WAIT_MS = 250
# A finished job nobody has collected is dropped after JOB_KEEP_S, and at
# most JOB_KEEP_FINISHED are kept at all: a Copy result is up to
# MAX_COPY_ROWS rows, held in memory until the tab picks it up.
JOB_KEEP_S = 600
JOB_KEEP_FINISHED = 8
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
# A CTE the query reads twice is computed once only when it is marked
# MATERIALIZED: SQLite flattens a plain SELECT into each place it is used,
# and so reads the table once per use. The hint is 3.35 syntax; an older
# SQLite gets the CTE inlined, which is the same answer at that cost.
_ONCE = "MATERIALIZED " if sqlite3.sqlite_version_info >= (3, 35, 0) else ""

OPERATORS = {
    "in": ("is any of", "many"),
    "not_in": ("is none of", "many"),
    "contains": ("contains", "one"),
    "not_contains": ("does not contain", "one"),
    "starts": ("starts with", "one"),
    "gt": ("> (numeric)", "one"),
    "lt": ("< (numeric)", "one"),
    "empty": ("is empty", "none"),
    "not_empty": ("is not empty", "none"),
}


def register(api):
    api.register_tab(
        id="firstlast",
        label="First/Last",
        entry="ui/tab.js",
        description=(
            "Group events (by host, user, anything) and produce the first and "
            "last row of each group with a templated description — logon "
            "sessions, tool-run windows, per-host activity bounds — as a new, "
            "taggable table."
        ),
    )
    api.register_api("meta", meta, methods=["GET"])
    api.register_api("values", values, methods=["POST"])
    api.register_api("preview", preview, methods=["POST"])
    api.register_api("rows", rows, methods=["POST"])
    api.register_api("create", create, methods=["POST"])
    api.register_api("job/start", job_start, methods=["POST"])
    api.register_api("job", job_status, methods=["GET"])
    api.register_api("job/cancel", job_cancel, methods=["POST"])


# --------------------------------------------------------------- helpers


def _source(req, body):
    if req.store is None:
        raise ValueError("Open a case first")
    try:
        source_id = int(body.get("source_id"))
    except (TypeError, ValueError):
        raise ValueError("source_id is required")
    try:
        return req.store.get_source(source_id)
    except KeyError:
        raise ValueError(f"No table {source_id}")


def _check_columns(src, columns):
    names = {c["name"] for c in src["columns"]}
    for col in columns:
        if col not in names:
            raise ValueError(f"No column {col!r} in {src['name']}")


def _derived_names(src):
    return {c["name"] for c in src["columns"] if c.get("derived")}


def _from_clause(src):
    base = f"{q(src['table_name'])} s"
    if not _derived_names(src):
        return base
    return f"{base} LEFT JOIN {q('drv_' + str(src['id']))} d ON d.rid = s.rid"


def _scope(req, src):
    """FROM text for reading this source's rows. A plain source is its
    table (plus sidecar); a merge is a UNION ALL of its members aliased
    `s`, carrying source_id, rid and every exposed column under plain
    names — so everything downstream references `s.<col>` uniformly
    (invariant #9: the merge path is part of the operation)."""
    if not src.get("is_merge"):
        return _from_clause(src)
    branches = [_member_select(req, src, req.store.get_source(mid)) for mid in src["member_source_ids"]]
    return "(" + union_all(branches) + ") s"


def _member_select(req, src, m):
    """One member of merge `src` as _scope's union reads it: source_id, rid
    and the merge's columns, NULL-padding the ones this member lacks."""
    sel = req.store.merge_select_list(src, m)
    return f"SELECT {int(m['id'])} AS source_id, s.rid AS rid, {sel} FROM {_from_clause(m)}"


def _col(src, name):
    if src.get("is_merge"):
        return f"s.{q(name)}"  # everything sits in the union subquery's alias
    return f"{'d' if name in _derived_names(src) else 's'}.{q(name)}"


def _esc_like(value):
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


NUM_RE_SQL = r"^-?\d+(\.\d+)?$"
_NUM_RE = re.compile(NUM_RE_SQL)


def _is_number_column(src, col):
    """Whether ingest typed this column as a number. Not _numeric() — that
    returns the cast expression, and any non-empty string is truthy."""
    return any(c["name"] == col and c.get("type") == "number" for c in src["columns"])


def _numeric(src, col):
    return _numeric_of(_col(src, col))


def _numeric_of(ident):
    return f"(CASE WHEN {ident} REGEXP '{NUM_RE_SQL}' THEN CAST({ident} AS REAL) ELSE NULL END)"


def _order_dir(number):
    """The direction the bookends' `ord` values (_groups_sql) list them in,
    first to last. A number column is ordered by value, as the grid's
    numeric sort orders it: its cells are TEXT, so "10" < "9" < "100", and
    a group of record ids came back First=10, Last=9. A cell that isn't a
    number (blank, "-", "n/a") must not win either end, so it sorts LAST
    both ways. SQLite sorts NULL first ascending and last descending, so
    ascending-NULLs-last is the negated value DESC (negation flips the
    order exactly and leaves NULL NULL); `NULLS LAST` needs SQLite 3.30.
    A text column's `ord` is its _row_key without the tie, which sorts as
    the value does, ascending."""
    return "DESC" if number else "ASC"


def _where(src, filters):
    clauses, params = [], []
    for f in filters or []:
        col, op = f.get("column"), f.get("op", "in")
        if op not in OPERATORS:
            raise ValueError(f"Unknown filter operator {op!r}")
        ident = _col(src, col)
        kind = OPERATORS[op][1]
        if kind == "many":
            vals = list(f.get("values") or [])
            if not vals:
                continue
            marks = ",".join("?" * len(vals))
            clauses.append(f"{ident} IN ({marks})" if op == "in"
                           else f"({ident} NOT IN ({marks}) OR {ident} IS NULL)")
            params.extend(vals)
        elif kind == "one":
            val = f.get("value")
            if val in (None, ""):
                continue
            if op == "contains":
                clauses.append(f"{ident} LIKE ? ESCAPE '\\'"); params.append(f"%{_esc_like(val)}%")
            elif op == "not_contains":
                clauses.append(f"({ident} NOT LIKE ? ESCAPE '\\' OR {ident} IS NULL)")
                params.append(f"%{_esc_like(val)}%")
            elif op == "starts":
                clauses.append(f"{ident} LIKE ? ESCAPE '\\'"); params.append(f"{_esc_like(val)}%")
            else:
                try:
                    number = float(val)
                except (TypeError, ValueError):
                    raise ValueError(f"{OPERATORS[op][0]} needs a number, got {val!r}")
                clauses.append(f"{_numeric(src, col)} {'>' if op == 'gt' else '<'} ?")
                params.append(number)
        else:
            clauses.append(f"({ident} IS NULL OR {ident} = '')" if op == "empty"
                           else f"({ident} IS NOT NULL AND {ident} != '')")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _tag_where(src, tags):
    """The tag filter's WHERE clause, or None. Tags live in the row_tags
    sidecar (keyed source_id, rid) in the same case file run_sql reads, so
    membership is one IN-subquery — no join, no new privileges. Everything
    interpolated is a validated int, so nothing here touches `params`."""
    if not tags:
        return None
    mode = tags.get("mode")
    if src.get("is_merge"):
        sub = "SELECT 1 FROM row_tags rt WHERE rt.source_id = s.source_id AND rt.rid = s.rid"
        if mode == "any":
            return f"EXISTS ({sub})"
        if mode == "none":
            return f"NOT EXISTS ({sub})"
        if mode == "ids":
            try:
                ids = [int(i) for i in (tags.get("ids") or [])]
            except (TypeError, ValueError):
                raise ValueError("Tag ids must be integers")
            if not ids:
                return None
            return f"EXISTS ({sub} AND rt.tag_id IN ({','.join(str(i) for i in ids)}))"
        raise ValueError(f"Unknown tag filter mode {mode!r}")
    sub = f"SELECT rid FROM row_tags WHERE source_id = {int(src['id'])}"
    if mode == "any":
        return f"s.rid IN ({sub})"
    if mode == "none":
        return f"s.rid NOT IN ({sub})"
    if mode == "ids":
        try:
            ids = [int(i) for i in (tags.get("ids") or [])]
        except (TypeError, ValueError):
            raise ValueError("Tag ids must be integers")
        if not ids:
            return None
        return f"s.rid IN ({sub} AND tag_id IN ({','.join(str(i) for i in ids)}))"
    raise ValueError(f"Unknown tag filter mode {mode!r}")


def _inline(sql, params):
    """run_sql takes no parameters — inline them, skipping both quoted-span
    kinds (see the pivot example: the numeric guard embeds `?` in a string
    literal, and a CSV header like "Elevated?" puts one in an identifier)."""
    out, i, k, n = [], 0, 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i:j + 1])
            i = j + 1
            continue
        if ch == "?":
            if k >= len(params):
                raise ValueError("more placeholders than parameters")
            val = params[k]
            k += 1
            out.append(repr(float(val)) if isinstance(val, (int, float)) and not isinstance(val, bool)
                       else "'" + str(val).replace("'", "''") + "'")
            i += 1
            continue
        out.append(ch)
        i += 1
    if k != len(params):
        raise ValueError("parameter/placeholder mismatch")
    return "".join(out)


# ------------------------------------------------------------ the query


def _validated(req, body):
    """Everything preview and create share: the source, the group columns,
    the sort column, the carried columns, the compiled WHERE, the template."""
    src = _source(req, body)
    group_cols = body.get("group_by") or []
    if not isinstance(group_cols, list) or not group_cols:
        raise ValueError("Pick at least one column to group by")
    if len(group_cols) > MAX_GROUP_COLS:
        raise ValueError(f"Too many group columns ({len(group_cols)} > {MAX_GROUP_COLS})")
    sort_col = body.get("sort_column")
    if not sort_col:
        raise ValueError("Pick the column that orders each group (first/last are meaningless without one)")
    carry = body.get("columns") or []
    sums = body.get("sum_columns") or []
    if not isinstance(sums, list):
        raise ValueError("sum_columns must be a list of column names")
    listed = body.get("value_columns") or []
    if not isinstance(listed, list):
        raise ValueError("value_columns must be a list of column names")
    uniq = body.get("unique_columns") or []
    if not isinstance(uniq, list):
        raise ValueError("unique_columns must be a list of column names")
    _check_columns(src, group_cols + [sort_col] + carry + sums + listed + uniq)
    for c in sums:
        # SUM over text is 0 in SQLite, and a 0 that looks like a real
        # total is the kind of number that ends up in a report. _numeric
        # builds a safe-cast EXPRESSION, not a predicate — the column's own
        # declared type is the question here.
        if not _is_number_column(src, c):
            raise ValueError(f"{c} is not a number column — nothing to sum")
    for f in body.get("filters") or []:
        _check_columns(src, [f.get("column")])
    where, params = _where(src, body.get("filters"))
    tag_clause = _tag_where(src, body.get("tags"))
    if tag_clause:
        where = f"{where} AND {tag_clause}" if where else f" WHERE {tag_clause}"
    template = body.get("template") or DEFAULT_TEMPLATE
    # {values:Column} takes any column, like {Column} does, so it is checked
    # here by name rather than waiting for _render to find nothing.
    for col in _template_value_columns(template):
        if col not in {c["name"] for c in src["columns"]}:
            raise ValueError(f"{{values:{col}}} names no column of {src['name']}")
    for col in _template_value_columns(template, "unique"):
        if col not in {c["name"] for c in src["columns"]}:
            raise ValueError(f"{{unique:{col}}} names no column of {src['name']}")
    # {sum:}/{min:}/{max:} take any number column too, not only one under
    # Total up: the template's ride the same grouping pass (see _result).
    # A text column is refused here for the reason Total up refuses it.
    for kind in AGGREGATES:
        for col in _template_value_columns(template, kind):
            if col not in {c["name"] for c in src["columns"]}:
                raise ValueError(f"{{{kind}:{col}}} names no column of {src['name']}")
            if not _is_number_column(src, col):
                raise ValueError(f"{{{kind}:{col}}} needs a number column — {col} is not one")
    row_json = bool(body.get("row_json"))
    return src, group_cols, sort_col, carry, sums, listed, uniq, where, params, template, row_json


def _sum_alias(col):
    """The output column's name. Prefixed so it cannot collide with a
    carried column of the same name sitting beside it."""
    return f"Sum of {col}"


# Group-wide aggregates for the {sum:Column} / {min:Column} / {max:Column}
# placeholders. Only a Total-up column's sum becomes an output column; the
# rest ride the same grouping pass for the description alone, so asking for
# one costs no extra pass and needs nothing under Total up.
AGGREGATES = {"sum": "SUM", "min": "MIN", "max": "MAX"}


def _agg_alias(kind, col):
    return _sum_alias(col) if kind == "sum" else f"{kind.capitalize()} of {col}"


def _unique_alias(col):
    """The Count unique output column's name, prefixed like the Sum column."""
    return f"Unique count of {col}"


def _reserved_aliases(sums, uniq=()):
    """The names the bookend query gives its own outputs. A source column
    that happens to share one must not be projected beside it: the inner
    SELECT would then carry the name twice — the file's, then the
    query's — and SQLite labels the outer SELECT *'s second copy
    `group_n:1`, so dict(zip(cols, row)) would hand _render and the Sum
    column the file's value in place of the group's, silently."""
    return {"rn_first", "rn_last", "group_n"} | {
        _agg_alias(kind, c) for c in sums for kind in AGGREGATES} | {_unique_alias(c) for c in uniq}


def _template_columns(template, src, sums=(), uniq=()):
    """The source columns a description template names as plain `{Name}`
    placeholders, in first-mention order. Tokenised exactly as _render
    does (a `{` runs to the next `}`), so what gets projected is what
    _render will look up; anything that is not a column — {which},
    {count}, the colon forms, a typo — is left for _render to resolve or
    refuse by name. A column named like one of the pass's own aliases is
    skipped (see _reserved_aliases): the template route must not shadow
    the query's outputs, so `{group_n}` reads the pass's count, not the
    file's column. The same column grouped or carried still shadows — a
    pre-existing edge, left as is; headers shaped like that are rare."""
    names = {c["name"] for c in src["columns"]} - _reserved_aliases(sums, uniq)
    out, i, n = [], 0, len(template or "")
    while i < n:
        if template[i] != "{":
            i += 1
            continue
        j = template.find("}", i + 1)
        if j == -1:
            break
        key = template[i + 1:j]
        if key in names and key not in out:
            out.append(key)
        i = j + 1
    return out


def _tie(src, alias):
    """The row's place in file order as fixed-width text, so it can ride on
    the end of a key that MIN and MAX compare as text: rid, or (source_id,
    rid) on a merge, where rid alone collides across members. 20 digits
    each holds any rid."""
    if src.get("is_merge"):
        return f"printf('%020d%020d', {alias}.source_id, {alias}.rid)"
    return f"printf('%020d', {alias}.rid)"


def _untie(src, key, prefix):
    """_tie's text back as [sid,] rid columns named `<prefix>_sid` and
    `<prefix>_rid`, read off the END of `key` so it can be a whole _row_key."""
    out = [f"CAST(substr({key}, -20) AS INTEGER) AS {prefix}_rid"]
    if src.get("is_merge"):
        out.insert(0, f"CAST(substr({key}, -40, 20) AS INTEGER) AS {prefix}_sid")
    return ", ".join(out)


def _row_key(src, ident):
    """A text key per row that sorts as `ORDER BY ident, <tie>` does, so a
    group's MIN of it is its first row and its MAX its last. hex() keeps the
    byte order the BINARY collation compares in and leaves no byte that can
    collide with the separator, and the space before the tie sorts below
    every hex digit, so a value that is a prefix of another stays before it.
    The first letter is the storage class in SQLite's own order: NULL sorts
    first ascending and last descending, which MIN and MAX then do too, and
    a BLOB after all text. Numbers, which SQLite sorts between NULL and
    text, never reach here: every source column has TEXT affinity, which
    stores a number as its text."""
    tie = _tie(src, "s")
    return (f"CASE WHEN {ident} IS NULL THEN 'A' || {tie}"
            f" WHEN typeof({ident}) = 'blob' THEN 'U' || hex({ident}) || ' ' || {tie}"
            f" ELSE 'T' || hex({ident}) || ' ' || {tie} END")


def _groups_sql(req, src, group_cols, sort_col, number, where, aggs, count_groups, uniq=()):
    """The CTEs behind `fl_groups`: one row per group with group_n, the
    first and last row's place (f_[sid,]rid, l_[sid,]rid) and the value
    they are listed by (f_ord, l_ord; see _order_dir), `pos` — where
    the last row stands in first-to-last order, which is what
    {which:number} prints for it — and each Total-up aggregate as agg<i>.
    With `count_groups` it is marked to be computed once, because the
    count of groups reads it a second time.

    Text and datetime columns take ONE grouping pass: _row_key folds the
    tie-break into the value, so the group's MIN and MAX are its first and
    last rows outright. A number column is ordered by the REAL its text
    casts to, and no text spells a REAL in order, so it takes two: the
    group's smallest and largest value, then a pass over the rows holding
    them for the one first in file order (`fl_rows` holds the
    REGEXP-guarded cast, done once per row; see _ONCE). A non-number cell
    sorts after every number at both ends, so it is first or last only in
    a group with no number at all.

    The window functions this replaced ranked every row in both directions
    and counted the groups, one sort of the whole table each — over every
    column the output needed. A grouping pass sorts once, on the group
    columns, and carries none of them."""
    keys = [f"k{i}" for i in range(len(group_cols))]
    keysel = ", ".join(f"{_col(src, g)} AS {k}" for g, k in zip(group_cols, keys))
    klist = ", ".join(keys)
    # Each Total-up input is cast once per row and aggregated from there.
    inputs = list(dict.fromkeys(c for c, _ in aggs))
    insel = "".join(f", {_numeric(src, c)} AS n{inputs.index(c)}" for c in inputs)
    aggsel = "".join(f", {fn}(n{inputs.index(c)}) AS agg{i}" for i, (c, fn) in enumerate(aggs))
    # Count unique rides the same pass: each column's raw value is carried
    # once per row and counted DISTINCT per group, blanks left out (as List
    # values leaves them out, and as the pivot's Distinct count does).
    insel += "".join(f", {_col(src, c)} AS u{j}" for j, c in enumerate(uniq))
    aggsel += "".join(f", COUNT(DISTINCT CASE WHEN u{j} != '' THEN u{j} END) AS uq{j}"
                      for j in range(len(uniq)))
    once = _ONCE if count_groups else ""
    if not number:
        inner = f"SELECT {keysel}, {_row_key(src, _col(src, sort_col))} AS key{insel} FROM {_scope(req, src)}{where}"
        # LIMIT keeps the subquery from being flattened into the GROUP BY
        # (SQLite won't flatten a LIMIT under an aggregate), so the key is
        # built once per row rather than once for MIN and again for MAX.
        tie = 40 if src.get("is_merge") else 20
        return (f"fl_groups AS {once}(SELECT COUNT(*) AS group_n, COUNT(*) AS pos,"
                f" {_untie(src, 'MIN(key)', 'f')}, {_untie(src, 'MAX(key)', 'l')},"
                f" substr(MIN(key), 1, length(MIN(key)) - {tie}) AS f_ord,"
                f" substr(MAX(key), 1, length(MAX(key)) - {tie}) AS l_ord{aggsel}"
                f" FROM ({inner} LIMIT -1) GROUP BY {klist})")
    sid = "s.source_id AS source_id, " if src.get("is_merge") else ""
    match = " AND ".join(f"b.{k} IS r.{k}" for k in keys)
    return (
        f"fl_rows AS {_ONCE}(SELECT {keysel}, {sid}s.rid AS rid, {_numeric(src, sort_col)} AS v{insel}"
        f" FROM {_scope(req, src)}{where}),"
        f" fl_bounds AS {_ONCE}(SELECT {klist}, COUNT(*) AS group_n, COUNT(v) AS nv, MIN(v) AS lo, MAX(v) AS hi,"
        f" MIN(CASE WHEN v IS NULL THEN {_tie(src, 'fl_rows')} END) AS nf,"
        f" MAX(CASE WHEN v IS NULL THEN {_tie(src, 'fl_rows')} END) AS nl{aggsel}"
        f" FROM fl_rows GROUP BY {klist}),"
        f" fl_ties AS (SELECT {', '.join(f'b.{k} AS {k}' for k in keys)},"
        f" MIN(CASE WHEN r.v = b.lo THEN {_tie(src, 'r')} END) AS tf,"
        f" MAX(CASE WHEN r.v = b.hi THEN {_tie(src, 'r')} END) AS tl"
        f" FROM fl_rows r JOIN fl_bounds b ON {match} AND r.v IN (b.lo, b.hi)"
        f" GROUP BY {', '.join(f'b.{k}' for k in keys)}),"
        f" fl_groups AS {once}(SELECT b.group_n AS group_n, CASE WHEN b.nv > 0 THEN b.nv ELSE b.group_n END AS pos,"
        f" {_untie(src, 'CASE WHEN b.nv > 0 THEN t.tf ELSE b.nf END', 'f')},"
        f" {_untie(src, 'CASE WHEN b.nv > 0 THEN t.tl ELSE b.nl END', 'l')},"
        f" CASE WHEN b.nv > 0 THEN -b.lo END AS f_ord, CASE WHEN b.nv > 0 THEN -b.hi END AS l_ord"
        f"{''.join(f', b.agg{i} AS agg{i}' for i in range(len(aggs)))}"
        f"{''.join(f', b.uq{j} AS uq{j}' for j in range(len(uniq)))}"
        f" FROM fl_bounds b LEFT JOIN fl_ties t ON {' AND '.join(f'b.{k} IS t.{k}' for k in keys)})"
    )


def _bookend_rows(req, src, group_cols, sort_col, carry, where, params, limit,
                  row_json=False, sums=(), template="", op_token=None, count_groups=False, uniq=()):
    """Each group's first and last row (one row, labelled Only, when they
    are the same row), ranked as `ORDER BY sort, <file order>` ranks them
    both ways, with the per-group values _emit and _render read: rn_first
    (the row's position first to last), rn_last (1 for the last row),
    group_n and the Total-up aggregates. Selected values are the *row's
    own* — the first row's user, not the group's. Returns (rows,
    truncated, total groups — counted only with `count_groups`).
    `op_token` (a job's id) makes the query stoppable through
    Store.cancel_op.

    The groups come from _groups_sql, which finds each bookend's place in
    the table; only then are the bookends' own columns read, by rid and only
    for the bookends within `limit`, so the table is never carried through a
    sort to find two rows a group."""
    # Every column the template or output might need, deduped, stable order
    # — or the whole row when the JSON cell is requested. The template's
    # own columns ride along so {Col} resolves for ANY column, not only
    # one that happens to be grouped, ordered or carried; they are
    # projected for _render alone and never become output columns.
    pool = ([c["name"] for c in src["columns"]] if row_json
            else group_cols + [sort_col] + carry + _template_columns(template, src, sums, uniq))
    needed = []
    for c in pool:
        if c not in needed:
            needed.append(c)
    # Totals ride the grouping pass that already counts each group, so they
    # cost no extra pass — the same reason group_n does. Both bookends carry
    # the total. _numeric's guarded cast, not a bare CAST: a number column
    # can still hold the odd "-" or "n/a", and CAST turns those into 0,
    # which is a wrong total rather than a missing one.
    # The sum is always an output column; min and max only exist for the
    # {min:}/{max:} placeholders, so they are added only when the template
    # actually names one.
    wanted = {"sum"} | {k for k in ("min", "max") if f"{{{k}:" in (template or "")}
    aggs = [(c, fn, _agg_alias(kind, c)) for c in sums for kind, fn in AGGREGATES.items() if kind in wanted]
    number = _is_number_column(src, sort_col)
    merge = bool(src.get("is_merge"))
    groups = _groups_sql(req, src, group_cols, sort_col, number, where,
                         [(c, fn) for c, fn, _ in aggs], count_groups, uniq)
    # One row per bookend. A group whose first row is its last gives one.
    # They are put in order and cut to the limit here, before any row is
    # read: Host + User on a 693k-row table is 486k groups, and Copy result
    # would otherwise look up nearly a million rows to keep 20,000.
    # The preview's count of groups reads fl_groups again, one row per
    # group. COUNT(*) OVER () there buffered every group to count them and
    # took two of the five seconds on 486k groups.
    counted = ", (SELECT COUNT(*) FROM fl_groups) AS total" if count_groups else ""
    same = "f_rid = l_rid" + (" AND f_sid = l_sid" if merge else "")
    order = f"ord {_order_dir(number)}, rn_first, {'sid, ' if merge else ''}rid"
    ends = (
        f"fl_ends AS (SELECT {'CASE WHEN w = 1 THEN f_sid ELSE l_sid END AS sid, ' if merge else ''}"
        f"CASE WHEN w = 1 THEN f_rid ELSE l_rid END AS rid,"
        f" CASE WHEN w = 1 THEN f_ord ELSE l_ord END AS ord,"
        f" CASE WHEN w = 1 THEN 1 ELSE pos END AS rn_first,"
        f" CASE WHEN w = 2 OR ({same}) THEN 1 ELSE pos END AS rn_last,"
        f" group_n{''.join(f', agg{i}' for i in range(len(aggs)))}"
        f"{''.join(f', uq{j}' for j in range(len(uniq)))}{counted}"
        f" FROM fl_groups CROSS JOIN (SELECT 1 AS w UNION ALL SELECT 2) WHERE w = 1 OR NOT ({same})),"
        f" fl_top AS (SELECT * FROM fl_ends ORDER BY {order} LIMIT {int(limit) + 1})"
    )
    if merge:
        return _merge_bookend_rows(req, src, needed, aggs, uniq, f"WITH {groups}, {ends}",
                                   params, order, limit, op_token, count_groups)
    # Laid out as the window query this replaced laid its rows out — the
    # columns, then rn_first, rn_last, group_n and the aggregates — so a
    # column named like one of those reads the same as it always did. What
    # follows is this query's own and is cut off before the rows are named.
    extra = ([", e.total"] if count_groups else []) + [", e.ord", ", e.rid"]
    cols = (", ".join(f"{_col(src, c)} AS {q(c)}" for c in needed)
            + ", e.rn_first AS rn_first, e.rn_last AS rn_last, e.group_n AS group_n"
            + "".join(f", e.agg{i} AS {q(alias)}" for i, (_, _, alias) in enumerate(aggs))
            + "".join(f", e.uq{j} AS {q(_unique_alias(c))}" for j, c in enumerate(uniq))
            + "".join(extra))
    # A CROSS JOIN keeps fl_top the outer loop, so each bookend is one
    # lookup by rid.
    target = f"{q(src['table_name'])} s ON s.rid = e.rid"
    if _derived_names(src):
        target += f" LEFT JOIN {q('drv_' + str(src['id']))} d ON d.rid = s.rid"
    fetch = f"SELECT {cols} FROM fl_top e CROSS JOIN {target}"
    # fl_top's order again, now that the lookups have shuffled it, by
    # position: a source column can have any of these names.
    width = len(needed) + 3 + len(aggs) + len(uniq) + len(extra)
    at = width - len(extra) + 1 + (1 if count_groups else 0)
    ties = ", ".join(str(i) for i in range(at + 1, width + 1))
    sql = (
        f"WITH {groups}, {ends} SELECT * FROM ({fetch})"
        f" ORDER BY {at} {_order_dir(number)}, {len(needed) + 1}, {ties}"
    )
    res = req.store.run_sql(_inline(sql, params), limit=limit, op_token=op_token)
    keep = width - len(extra)
    cols = res["columns"][:keep]
    total = res["rows"][0][keep] if count_groups and res["rows"] else 0
    return [dict(zip(cols, r[:keep])) for r in res["rows"]], res["truncated"], total


def _merge_bookend_rows(req, src, needed, aggs, uniq, with_groups, params, order, limit,
                        op_token, count_groups):
    """_bookend_rows for a merge, in two queries: the bookends' places
    (member, rid and the per-group values) in order, then their columns,
    each member's looked up in its own table by rid.

    Not one query, as a single table's is, because a merge's lookup is a
    UNION ALL with an arm per member, and an arm that joined fl_top made
    SQLite expand fl_top, and with it the whole union beneath it, once per
    arm: members squared references to every table, past SQLite's 65,535
    from 120 to 250 members by the options ("too many references to
    row_tags"). Joining
    fl_top to _scope's union instead would read every member whole to find
    two rows a group."""
    per_group = (["rn_first", "rn_last", "group_n"] + [f"agg{i}" for i in range(len(aggs))]
                 + [f"uq{j}" for j in range(len(uniq))] + (["total"] if count_groups else []))
    sql = f"{with_groups} SELECT sid, rid, {', '.join(per_group)} FROM fl_top ORDER BY {order}"
    top = req.store.run_sql(_inline(sql, params), limit=limit, op_token=op_token)
    places = top["rows"]
    if not places:
        return [], top["truncated"], 0
    rids = {}
    for sid, rid, *_ in places:
        rids.setdefault(sid, []).append(rid)
    # Selected under the single-table query's names, NULL standing in for
    # the per-group values, and through `SELECT *`, so SQLite names a
    # repeated one (a column called group_n) as it does there: group_n:1.
    keep = len(per_group) - (1 if count_groups else 0)
    sel = (", ".join(f"{_col(src, c)} AS {q(c)}" for c in needed)
           + ", NULL AS rn_first, NULL AS rn_last, NULL AS group_n"
           + "".join(f", NULL AS {q(alias)}" for _, _, alias in aggs)
           + "".join(f", NULL AS {q(_unique_alias(c))}" for c in uniq))
    arms = [f"SELECT {sel}, {int(sid)}, s.rid FROM ({_member_select(req, src, req.store.get_source(sid))}) s"
            f" WHERE s.rid IN ({', '.join(str(int(r)) for r in sorted(member))})"
            for sid, member in sorted(rids.items())]
    res = req.store.run_sql(f"SELECT * FROM ({union_all(arms)})", limit=len(places), op_token=op_token)
    names = res["columns"][:len(needed) + keep]
    found = {(r[-2], r[-1]): r[:len(needed)] for r in res["rows"]}
    rows = [dict(zip(names, list(found[(sid, rid)]) + rest[:keep]))
            for sid, rid, *rest in places if (sid, rid) in found]
    total = places[0][-1] if count_groups else 0
    return rows, top["truncated"], total


def _values_alias(col):
    """The List values output column's name, prefixed like the Sum column
    so it cannot collide with a carried column of the same name."""
    return f"Values of {col}"


def _template_value_columns(template, kind="values"):
    """The columns `{values:Column}` (or `{<kind>:Column}`) names, in
    first-mention order — tokenised the way _render does it."""
    out, i, n = [], 0, len(template or "")
    while i < n:
        if template[i] != "{":
            i += 1
            continue
        j = template.find("}", i + 1)
        if j == -1:
            break
        key = template[i + 1:j]
        if key.startswith(kind + ":"):
            col = key.split(":", 1)[1].strip()
            if col not in out:
                out.append(col)
        i = j + 1
    return out


# Joins a group's values inside SQLite and splits them back apart in
# Python. A unit separator, because commas, tabs and newlines all turn up
# in real cells; this one does not.
_VALUE_SEP = "\x1f"


def _group_values(req, src, group_cols, where, params, cols, bookends, op_token=None):
    """{column: {group key: cell}} — each group's distinct, non-blank values
    of each column, sorted (numbers by value), at most MAX_LISTED_VALUES
    named and the rest counted. A group key is the tuple of the bookend's
    own group values, so it matches the row it is attached to whatever the
    group columns hold, NULL included.

    Only the groups in `bookends` are needed. A handful (the preview) are
    asked for by value, which keeps the preview from listing every group in
    the table; a whole result reads every group, and a cap hit there is an
    error rather than a silently empty cell."""
    if not cols or not bookends:
        return {}
    keys = {tuple(r.get(g) for g in group_cols) for r in bookends}
    gexprs = [_col(src, g) for g in group_cols]
    kcols = ", ".join(f"k{i}" for i in range(len(group_cols)))
    restrict, rparams = "", []
    if len(keys) <= 4 * PREVIEW_GROUPS:
        ors = []
        for key in keys:
            parts = []
            for expr, v in zip(gexprs, key):
                if v is None:
                    parts.append(f"{expr} IS NULL")
                else:
                    parts.append(f"{expr} = ?")
                    rparams.append(v)
            ors.append("(" + " AND ".join(parts) + ")")
        restrict = "(" + " OR ".join(ors) + ")"
    out = {}
    for col in cols:
        ident = _col(src, col)
        conds = [f"{ident} IS NOT NULL", f"{ident} != ''"] + ([restrict] if restrict else [])
        scoped = f"{where} AND " if where else " WHERE "
        number = _is_number_column(src, col)
        # Sorted in SQL too, so the values a capped cell names are the
        # first ones in order rather than whichever came up first.
        order = (f"({_numeric_of('v')} IS NULL), {_numeric_of('v')}, v" if number
                 else "v COLLATE NOCASE, v")
        sel = ", ".join(f"{e} AS k{i}" for i, e in enumerate(gexprs))
        sql = (
            f"SELECT {kcols}, group_concat(v, char(31)), MAX(n) FROM ("
            f"SELECT {kcols}, v, COUNT(*) OVER (PARTITION BY {kcols}) AS n,"
            f" ROW_NUMBER() OVER (PARTITION BY {kcols} ORDER BY {order}) AS rn"
            f" FROM (SELECT DISTINCT {sel}, {ident} AS v FROM {_scope(req, src)}"
            f"{scoped}{' AND '.join(conds)})"
            f") WHERE rn <= {MAX_LISTED_VALUES} GROUP BY {kcols}"
        )
        res = req.store.run_sql(_inline(sql, params + rparams), limit=MAX_GROUPS, op_token=op_token)
        cells = {}
        for row in res["rows"]:
            key = tuple(row[:len(group_cols)])
            joined, total = row[len(group_cols)], row[len(group_cols) + 1]
            vals = [] if joined is None else str(joined).split(_VALUE_SEP)
            vals.sort(key=_value_sort_key(number))
            cell = ", ".join(vals)
            if total and total > len(vals):
                cell += f" … (+{total - len(vals):,} more)"
            cells[key] = cell
        if res["truncated"] and not keys.issubset(cells):
            raise ValueError(f"More than {MAX_GROUPS:,} groups — too many to list "
                             f"{col} for; narrow the grouping or add a filter")
        out[col] = cells
    return out


def _value_sort_key(number):
    """The SQL ORDER BY above, again in Python — group_concat's order is
    not one SQLite promises."""
    def key(v):
        if number:
            return (0, float(v), v) if _NUM_RE.match(v) else (1, 0.0, v)
        return (v.casefold(), v)
    return key


def _render(template, row, which, sums=()):
    """{ColumnName} → the row's value, {count} → group size, {which} →
    First/Last/Only, {which:number} → the row's position in its group,
    {sum:Column} / {min:Column} / {max:Column} → a number column's group
    total, smallest and largest, formatted like the Sum column (`sums` is
    every column the pass totalled), {values:Column} → the group's
    distinct values of any column (attached by _attach_values),
    {unique:Column} → how many there are. Unknown names raise, naming the
    offender.

    The colon form is the namespace for anything computed rather than
    read: a plain name is always a field, so no column name can shadow a
    function and no function can shadow a column."""
    out, i, n = [], 0, len(template)
    while i < n:
        ch = template[i]
        if ch == "{":
            j = template.find("}", i + 1)
            if j == -1:
                raise ValueError("Unclosed { in the description template")
            key = template[i + 1:j]
            if key == "which":
                out.append(which)
            elif key == "which:number":
                # rn_first IS the position: 1 for the first, the group size
                # for the last. Not group_n for the last — a number column's
                # non-number cells sort after it, and then it is not Nth.
                out.append(str(row.get("rn_first", "")))
            elif key.startswith("values:"):
                out.append(row.get(("values", key.split(":", 1)[1].strip()), ""))
            elif key.startswith("unique:"):
                got = row.get(_unique_alias(key.split(":", 1)[1].strip()))
                out.append("" if got is None else str(got))
            elif key == "count":
                out.append(str(row.get("group_n", "")))
            elif ":" in key and key.split(":", 1)[0] in AGGREGATES:
                kind, col = key.split(":", 1)
                col = col.strip()
                if col not in sums:
                    # _validated checks these and _result totals them, so
                    # this is a caller that skipped one or the other.
                    raise ValueError(f"{{{key}}} needs a number column of this table")
                out.append(_fmt_sum(row.get(_agg_alias(kind, col))))
            elif key in row:
                val = row.get(key)
                out.append("" if val is None else str(val))
            else:
                raise ValueError(f"Unknown placeholder {{{key}}} — use a column name, {{count}}, {{which}}, "
                                 f"{{which:number}}, {{values:Column}}, {{unique:Column}}, {{sum:Column}}, {{min:Column}} "
                                 f"or {{max:Column}}")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


ROW_JSON_COLUMN = "Row (JSON)"


def _fmt_sum(v):
    """Whole numbers without a trailing .0 — a count of bytes should read
    as bytes. SUM is REAL so that mixed ints and decimals add up."""
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else f"{float(v):g}"


def _attach_values(bookends, group_cols, listed):
    """Hang each group's List values cells (from _group_values) on its
    bookends, under a tuple key no column name can be."""
    for r in bookends:
        key = tuple(r.get(g) for g in group_cols)
        for col, cells in listed.items():
            r[("values", col)] = cells.get(key, "")


def _header(sort_col, carry, sums, listed, row_json, uniq=()):
    """The output columns, in the order _emit fills them. The description
    sits before the JSON cell: it is the line an analyst reads, and the
    JSON is the long cell they open when they need it."""
    return ([sort_col] + carry + [_sum_alias(c) for c in sums]
            + [_unique_alias(c) for c in uniq]
            + [_values_alias(c) for c in listed] + ["Description"]
            + ([ROW_JSON_COLUMN] if row_json else []))


def _emit(rows, sort_col, carry, template, json_cols=None, sums=(), listed=(), uniq=(), totalled=None):
    """The output rows: one per bookend. A single-row group is both its own
    first and its last — emitted once, labelled Only (a story with one
    event has no separate ending, and "First of 1" read as if a Last had
    gone missing). `json_cols` non-None adds a cell with the bookend's
    ENTIRE row as a JSON object — the synthetic columns
    (rn_first/rn_last/group_n) and the attached value lists never appear
    in it."""
    out = []
    for r in rows:
        labels = []
        if r.get("rn_first") == 1 and r.get("rn_last") == 1:
            labels.append("Only")
        elif r.get("rn_first") == 1:
            labels.append("First")
        elif r.get("rn_last") == 1:
            labels.append("Last")
        for which in labels:
            desc = _render(template, r, which, sums if totalled is None else totalled)
            row = ([r.get(sort_col, "")]
                   + [("" if r.get(c) is None else str(r.get(c))) for c in carry]
                   + [_fmt_sum(r.get(_sum_alias(c))) for c in sums]
                   + [str(r.get(_unique_alias(c)) or 0) for c in uniq]
                   + [r.get(("values", c), "") for c in listed]
                   + [desc])
            if json_cols is not None:
                row.append(json.dumps({c: r.get(c) for c in json_cols}, ensure_ascii=False))
            out.append(row)
    return out


# ---------------------------------------------------------------- routes


def meta(req):
    return {
        "operators": [{"id": k, "label": v[0], "value_kind": v[1]} for k, v in OPERATORS.items()],
        "limits": {"groups": MAX_GROUPS, "group_cols": MAX_GROUP_COLS, "preview_groups": PREVIEW_GROUPS},
        "placeholders": ["which", "which:number", "count", "values:<column>", "unique:<column>",
                         "sum:<column>", "min:<column>", "max:<column>"],
        "which_values": ["First", "Last", "Only"],
    }


def values(req):
    body = req.body or {}
    src = _source(req, body)
    column = body.get("column")
    _check_columns(src, [column])
    limit = min(int(body.get("limit") or 500), 5000)
    sql = (f"SELECT {_col(src, column)} AS value, COUNT(*) AS n FROM {_scope(req, src)}"
           f" GROUP BY 1 ORDER BY n DESC")
    res = req.store.run_sql(sql, limit=limit)
    return {"values": [{"value": r[0], "count": r[1]} for r in res["rows"]],
            "truncated": res["truncated"]}


def _result(req, body, limit, *, whole=False, op_token=None, count_groups=False):
    """What preview, rows and create share: validate, find the bookends,
    list the distinct values it asks for, render. `whole` is create's
    rule that a result cut short by `limit` is refused, not written;
    `count_groups` is the preview's, which says how many groups there are.
    `op_token` rides on every query, so a job's cancel stops whichever one
    is running."""
    (src, group_cols, sort_col, carry, sums, listed, uniq,
     where, params, template, row_json) = _validated(req, body)
    # The zone's columns become output columns; {unique:Column} counts any
    # column, so the template's ride the same pass without one.
    counted = list(dict.fromkeys(uniq + _template_value_columns(template, "unique")))
    # Likewise totals: Total up's columns are output columns, and any number
    # column a {sum:}/{min:}/{max:} names is totalled for the description.
    totalled = list(dict.fromkeys(
        sums + [c for kind in AGGREGATES for c in _template_value_columns(template, kind)]))
    bookends, truncated, total_groups = _bookend_rows(
        req, src, group_cols, sort_col, carry, where, params, limit=limit, row_json=row_json,
        sums=totalled, template=template, op_token=op_token, count_groups=count_groups, uniq=counted)
    if whole and truncated:
        raise ValueError(f"More than {MAX_GROUPS:,} groups — narrow the grouping or add a filter")
    wanted = list(dict.fromkeys(listed + _template_value_columns(template)))
    _attach_values(bookends, group_cols,
                   _group_values(req, src, group_cols, where, params, wanted, bookends,
                                 op_token=op_token))
    json_cols = [c["name"] for c in src["columns"]] if row_json else None
    return {
        "src": src, "group_cols": group_cols, "where": where, "params": params,
        "columns": _header(sort_col, carry, sums, listed, row_json, uniq),
        "rows": _emit(bookends, sort_col, carry, template, json_cols, sums, listed, uniq, totalled),
        "truncated": truncated, "total_groups": total_groups,
    }


def preview(req):
    """POST .../preview — the first PREVIEW_GROUPS groups' bookends, plus the
    total group count, so 'Create table' says what it will make before it
    makes it."""
    return _preview(req, req.body or {})


def _preview(req, body, op_token=None, stopped=None):
    # The group count comes out of the same pass that finds the bookends;
    # it used to be a GROUP BY of its own, a second read of the table.
    res = _result(req, body, PREVIEW_GROUPS * 2 + 2, op_token=op_token, count_groups=True)
    return {"columns": res["columns"], "rows": res["rows"], "total_groups": res["total_groups"]}


def rows(req):
    """POST .../rows — the ENTIRE result as rows, for "Copy result": the
    preview shows only the first few groups, and copying should hand over
    what Create would have made, not what happened to be on screen."""
    return _rows(req, req.body or {})


def _rows(req, body, op_token=None, stopped=None):
    res = _result(req, body, MAX_COPY_ROWS, op_token=op_token)
    return {"columns": res["columns"], "rows": res["rows"], "truncated": res["truncated"]}


def create(req):
    """POST .../create — run the whole thing and land it as a new source.

    `timeline_tag` (optional, a tag name) puts the bookends on the unified
    Timeline: the Timeline is exactly "every tagged row across the case",
    so a tag IS the membership. The tag is found or created by name and
    applied to every row of the new table through set_tags — the public
    path that routes _apply_tag_change (invariant #7), so undo works on it
    like any other tag write. Rids are contiguous from 1 on every ingest
    path (invariant #2), which is what makes range(1, n+1) exact."""
    return _create(req, req.body or {})


def _create(req, body, op_token=None, stopped=None):
    """`stopped` is a job's cancel flag, asked once the result is computed:
    the queries before it are interrupted through `op_token`, but nothing
    interrupts ingest_rows, so a cancel that lands after the queries is
    honoured only if it lands before the table is written. One that comes
    later is too late — the job finishes and says what it made."""
    res = _result(req, body, MAX_GROUPS * 2, whole=True, op_token=op_token)
    if stopped is not None and stopped():
        raise OpCancelled("Cancelled")
    src, header, out_rows = res["src"], res["columns"], res["rows"]
    # The table's label as the analyst knows it — its nickname when it has
    # one, as everywhere else in the app.
    name = (body.get("name") or "").strip() or f"First-Last of {src.get('nickname') or src['name']}"
    rec = req.store.ingest_rows(header, out_rows, name=name)
    out = {"source": {"id": rec["id"], "name": rec["name"], "row_count": rec["row_count"]}}
    tag_name = (body.get("timeline_tag") or "").strip()
    if tag_name:
        tag = next((t for t in req.store.list_tags() if t["name"] == tag_name), None)
        if tag is None:
            tag = req.store.upsert_tag(None, tag_name, TIMELINE_TAG_COLOR, None)
        req.store.set_tags(rec["id"], list(range(1, rec["row_count"] + 1)), tag["id"], True)
        out["timeline_tag"] = {"id": tag["id"], "name": tag["name"]}
    return out


# ------------------------------------------------------------------ jobs

# preview, rows and create as the tab runs them: on a thread, polled, and
# stoppable. The module docstring says why.
JOB_KINDS = {"preview": _preview, "rows": _rows, "create": _create}

_jobs = {}                      # job id -> the record job_start builds
_jobs_lock = threading.Lock()


def _job_id(value):
    """A job's id. The client makes it, as the app makes its op tokens, so
    a cancel can name a job whose start has not answered yet; it is held
    to a plain shape because it doubles as the job's op token."""
    jid = str(value or "")
    if not JOB_ID_RE.match(jid):
        raise ValueError("A job id is 8 to 80 letters, digits, - or _")
    return jid


def _prune_jobs(now):
    """Under _jobs_lock. A finished job nobody collected goes after
    JOB_KEEP_S, or once JOB_KEEP_FINISHED newer ones have finished. A
    running job is never dropped: its thread still has to land its result
    somewhere a poll can find it."""
    finished = sorted((j for j in _jobs.values() if j["status"] != "running"),
                      key=lambda j: j["finished"], reverse=True)
    for i, j in enumerate(finished):
        if i >= JOB_KEEP_FINISHED or now - j["finished"] > JOB_KEEP_S:
            del _jobs[j["id"]]


def _job_view(job):
    """What start and a poll answer. A finished job's result (or error) is
    handed over once and the job is dropped with it — a Copy result can be
    thousands of rows, and nothing asks twice."""
    with _jobs_lock:
        end = job["finished"] or time.monotonic()
        out = {"id": job["id"], "kind": job["kind"], "status": job["status"],
               "elapsed_ms": int((end - job["started"]) * 1000)}
        if job["status"] == "done":
            out["result"] = job["result"]
        elif job["status"] != "running":
            out["error"] = job["error"]
        if job["status"] != "running":
            _jobs.pop(job["id"], None)
    return out


def _run_job(job, req, spec):
    """The job's thread: the synchronous route's own code, under the job's
    id as op token and with its cancel flag, and the outcome recorded the
    way the route would have answered it — ValueError is the analyst's to
    fix, OpCancelled is a cancel (or the case closing under it)."""
    result, error = None, None
    try:
        result = JOB_KINDS[job["kind"]](req, spec, op_token=job["id"], stopped=job["stop"].is_set)
        status = "done"
    except OpCancelled as e:
        status, error = "cancelled", str(e) or "Cancelled"
    except ValueError as e:
        status, error = "error", str(e)
    except Exception as e:  # noqa: BLE001 — a thread has nobody to raise to
        # The route would have been a 500 with this traceback on the
        # console; the job keeps both halves of that.
        traceback.print_exc()
        status, error = "error", f"{type(e).__name__}: {e}"
    with _jobs_lock:
        job.update(status=status, result=result, error=error, finished=time.monotonic())
    job["done"].set()


def job_start(req):
    """POST .../job/start {id, kind, spec[, wait_ms]} — run `kind` (preview,
    rows or create) over `spec`, the very body that route takes, on a
    thread. Answers within `wait_ms` (default JOB_START_WAIT_MS, at most
    2 s): the finished job when it was that quick, {status: "running"}
    otherwise, to be polled at GET job. The spec is validated here first,
    so a grouping the route would refuse is the same 400, now, rather than
    a job that fails a moment later."""
    body = req.body or {}
    jid = _job_id(body.get("id"))
    kind = body.get("kind")
    if kind not in JOB_KINDS:
        raise ValueError(f"Unknown job kind {kind!r} — one of {', '.join(JOB_KINDS)}")
    spec = body.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object: the body the preview, rows or create route takes")
    _validated(req, spec)
    try:
        wait_ms = max(0, min(int(body.get("wait_ms", JOB_START_WAIT_MS)), 2000))
    except (TypeError, ValueError):
        raise ValueError("wait_ms must be a number of milliseconds")
    now = time.monotonic()
    job = {"id": jid, "kind": kind, "store": req.store, "status": "running",
           "result": None, "error": None, "started": now, "finished": None,
           "done": threading.Event(), "stop": threading.Event()}
    with _jobs_lock:
        _prune_jobs(now)
        if jid in _jobs:
            raise ValueError(f"There is already a job {jid}")
        _jobs[jid] = job
    threading.Thread(target=_run_job, args=(job, req, spec), daemon=True,
                     name=f"first-last {kind} {jid}").start()
    job["done"].wait(wait_ms / 1000)
    return _job_view(job)


def job_status(req):
    """GET .../job?id= — the job's state, and a finished job's result or
    error, handed over once. A job already collected, expired, or started
    in another case is gone, and the 400 says so."""
    jid = _job_id(req.query.get("id"))
    with _jobs_lock:
        job = _jobs.get(jid)
    if job is None or job["store"] is not req.store:
        raise ValueError("No such First/Last job: it was collected already, expired, "
                         "or belongs to another case")
    return _job_view(job)


def job_cancel(req):
    """POST .../job/cancel {id} — stop a job. The query it is running is
    interrupted (Store.cancel_op on its id) and create looks at its flag
    once more before it writes the table. Safe on a job that has finished,
    and on one whose start has not arrived: cancel_op marks the token
    either way, so a start that lands later stops at its first query."""
    jid = _job_id((req.body or {}).get("id"))
    with _jobs_lock:
        job = _jobs.get(jid)
        running = job is not None and job["status"] == "running"
    if job is not None:
        job["stop"].set()
    store = job["store"] if job is not None else req.store
    if store is not None and not getattr(store, "closed", False):
        store.cancel_op(jid)
    return {"cancelled": running}
