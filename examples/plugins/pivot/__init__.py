"""Pivot table plugin for Winnow — Excel's PivotTable over an ingested table.

Drag fields into Rows / Columns / Values / Filters and get a cross-tab with
subtotals and grand totals: "4624s per host per day", "distinct users per
process", "bytes transferred per destination". The tab is the plugin's own ES
module (register_tab); this file is the backend it queries (register_api).

Three things about the shape of this, since they're the parts that aren't
obvious:

**It aggregates the whole table, not the grid's current view.** A view is a
temp table in Winnow's scratch database, which `run_sql`'s read-only
connection to the case file can't see — and reaching for the private reader
that could is exactly what the plugin guide says not to do. That's why the
Filters area exists: it's the pivot's own filtering, the way Excel's is, and
it compiles into the same WHERE clause the aggregation runs under. Its tag
chips are in that clause too: tags are a sidecar in the case file
(`row_tags`), not part of a view, so `run_sql` reads them directly.

**Subtotals come from grouping sets, not from re-adding the cells.** Ask for
a Count Distinct subtotal and summing the child cells gives you the wrong
answer whenever a value appears under more than one child. So the client
declares every grouping level it wants to draw, and all of them come out of
ONE statement that reads the table once (_rollup_sql): a base CTE groups the
rows by every key any level uses, keeping partial aggregates that do add up
(counts, sums, mins, maxes; an average as its sum and count), and each level
is a GROUP BY over that base rather than over the table. A Distinct count is
the one that doesn't add up, so its column joins the base's keys instead, and
each level counts distinct values among the base rows. That is the same
answer as aggregating each level from the source, for every aggregation, at
the cost of one table scan rather than one per level — which on a wide table
is most of the time spent, and made a pivot several times slower than the
same GROUP BY typed into the SQL pane.

**Sum/Average/numeric Min/Max go through the same guarded cast the rest of
Winnow uses.** Columns are stored as TEXT (evidence fidelity), and SQLite's
bare `CAST(text AS REAL)` turns "N/A" into 0.0 — indistinguishable from a
real zero in a total. NUM_RE gates the cast so junk becomes NULL and drops
out of the aggregate instead of quietly dragging an average down.
"""

from winnow.store import NUM_RE, q, union_all

PLUGIN = {
    "name": "pivot-table",
    "version": "1.0.0",
    "description": "Excel-style pivot tables: drag fields into rows, columns and values for cross-tabs with subtotals.",
}

WINNOW_API_VERSION = 13   # winnow.dropdownMenu: the click-to-place menu under a field

# Grouping levels per refresh: one UNION ALL arm each over the base. Rows ×
# columns levels stay small in practice (Excel's own field wells get
# unusable well before this), and the cap bounds the statement a
# pathological request can build.
MAX_GROUP_SETS = 16
# Distinct groups returned per level. A pivot with more rows than this isn't
# readable anyway — the answer is another field in Filters, not more rows.
MAX_GROUPS = 20_000
# Rows shown by the drill-down ("show details" on a cell).
MAX_DETAIL_ROWS = 500

AGGREGATIONS = {
    # id: (label, needs_column)
    "count": ("Count", False),
    "count_distinct": ("Distinct count", True),
    "sum": ("Sum", True),
    "avg": ("Average", True),
    "min": ("Min", True),
    "max": ("Max", True),
}

# Filter operators, mirroring the header filter box's vocabulary so an
# analyst doesn't learn a second one. value_kind: 'one' | 'many' | 'none'.
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

# The tag filter, in the grid's own spelling (S.tagFilter, compiled by
# Store._compile_where) so the chips mean here what they mean on the grid's
# ribbon: a tag id keeps rows carrying that tag, TAG_ANY rows carrying any
# tag, TAG_NONE rows carrying none — those three OR'd, because two chips
# mean rows carrying either — and "!<id>" drops rows carrying that tag,
# AND'd onto everything else, so "Suspicious, but not Benign" loses a row
# that carries both.
TAG_ANY, TAG_NONE = "__any__", "__none__"


def register(api):
    api.register_tab(
        id="pivot",
        label="Pivot",
        entry="ui/tab.js",
        description=(
            "Excel-style pivot table: drag fields into Rows, Columns and Values "
            "for a cross-tab with subtotals, then click a cell to see the rows "
            "behind it."
        ),
    )
    api.register_api("meta", meta, methods=["GET"])
    api.register_api("aggregate", aggregate, methods=["POST"])
    api.register_api("values", values, methods=["POST"])
    api.register_api("detail", detail, methods=["POST"])


# --------------------------------------------------------------- helpers


def _source(req, body):
    """The validated source for this request — a real table or a merge
    (which reads through _scope's UNION ALL of its members)."""
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
    """Every column name is analyst-supplied (it round-trips through the
    browser), so nothing reaches SQL that isn't a real column of this table."""
    names = {c["name"] for c in src["columns"]}
    for col in columns:
        if col not in names:
            raise ValueError(f"No column {col!r} in {src['name']}")


def _derived_names(src):
    """The analyst's own added columns. They are NOT in `src_<id>` — they're
    materialised in the `drv_<id>` sidecar and merged into `src["columns"]`
    at read time — so any SQL that names one has to join that sidecar and
    qualify the reference. Getting this wrong is silent, not loud: SQLite's
    double-quoted-string fallback turns an unknown `"Day"` into the *string*
    'Day', so a pivot grouped on a derived column reports one group named
    after the column instead of failing."""
    return {c["name"] for c in src["columns"] if c.get("derived")}


def _from_clause(src):
    """`src_<id> s`, plus the derived sidecar when this source has one.

    LEFT JOIN, and `drv_<id>.rid` is an INTEGER PRIMARY KEY, so the join
    matches at most one row and can't change what's being counted — the same
    property Store._from_clause relies on."""
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
    branches = []
    for mid in src["member_source_ids"]:
        m = req.store.get_source(mid)
        sel = req.store.merge_select_list(src, m)  # NULL-pads columns a member lacks
        branches.append(f"SELECT {int(m['id'])} AS source_id, s.rid AS rid, {sel} FROM {_from_clause(m)}")
    return "(" + union_all(branches) + ") s"


def _col(src, name):
    """A column reference for this source's FROM clause."""
    if src.get("is_merge"):
        return f"s.{q(name)}"  # everything sits in the union subquery's alias
    return f"{'d' if name in _derived_names(src) else 's'}.{q(name)}"


def _numeric(src, col):
    """CAST guarded by the same pattern ingest-time type inference uses, so
    non-numeric text aggregates as NULL rather than as 0.0."""
    ident = _col(src, col)
    return f"(CASE WHEN {ident} REGEXP '{NUM_RE.pattern}' THEN CAST({ident} AS REAL) ELSE NULL END)"


def _agg_expr(src, measure, coltypes):
    """SQL for one Values field. Min/Max stay textual for non-numeric columns
    on purpose: the earliest and latest value of an ISO timestamp column is
    exactly what MIN()/MAX() over its text gives you, and casting it to a
    number would throw the whole column away."""
    agg = measure.get("agg", "count")
    if agg not in AGGREGATIONS:
        raise ValueError(f"Unknown aggregation {agg!r}")
    column = measure.get("column")
    if agg == "count":
        # Excel's Count is COUNTA — how many rows have a value in *this*
        # field, not how many rows there are. The difference is the whole
        # point of putting a specific field in Values: "Count of Bytes" over
        # a column that's half blank must not read the same as "Count of
        # Host". SQLite's COUNT(col) only skips NULL, and a missing value in
        # an ingested table is '' (ragged rows are padded, not nulled), so
        # the blank test has to be explicit.
        if not column:
            return "COUNT(*)"
        ident = _col(src, column)
        return f"SUM(CASE WHEN {ident} IS NOT NULL AND {ident} != '' THEN 1 ELSE 0 END)"
    if not column:
        raise ValueError(f"{AGGREGATIONS[agg][0]} needs a column")
    ident = _col(src, column)
    if agg == "count_distinct":
        # Blank is the absence of a value, not one more distinct value — the
        # same call Count makes two lines up. Counting '' as a category makes
        # "Distinct count of User" exceed "Count of User" on a column with
        # any padding in it, which reads as a bug because it is one.
        return f"COUNT(DISTINCT CASE WHEN {ident} IS NOT NULL AND {ident} != '' THEN {ident} END)"
    if agg in ("min", "max") and coltypes.get(column) != "number":
        return f"{agg.upper()}({ident})"
    return f"{agg.upper()}({_numeric(src, column)})"


def _rollup_measure(src, measure, coltypes, j, base_alias):
    """How one Values field travels through _rollup_sql's base: the partial
    aggregates the base keeps per group, `roll` — its answer for a level
    that groups several base rows together — and `same`, its answer for a
    level whose keys are the base's own, where each base row already is one
    group and re-grouping would only sort the base again.

    Every partial is _agg_expr's own SQL (or, for an average, the sum and
    count it divides), so a cell computed through the base is the same
    expression over the same rows as one computed from the source."""
    agg = measure.get("agg", "count")
    p = f"p{j}"
    if agg == "count_distinct":
        # Distinct counts don't add up, so nothing partial is kept: the
        # column is one of the base's keys, and a level counts the distinct
        # values among its base rows — the same blank rule _agg_expr uses.
        k = base_alias[measure["column"]]
        present = f"{k} IS NOT NULL AND {k} != ''"
        return [], f"COUNT(DISTINCT CASE WHEN {present} THEN {k} END)", f"(CASE WHEN {present} THEN 1 ELSE 0 END)"
    if agg == "avg":
        num = _numeric(src, measure["column"])
        # REAL over INTEGER, so the division is never integer division; a
        # group with no numeric value has a NULL sum, so its average is NULL
        # as AVG's would be.
        return ([(f"{p}s", f"SUM({num})"), (f"{p}n", f"COUNT({num})")],
                f"SUM({p}s) / SUM({p}n)", f"{p}s / {p}n")
    roll = {"count": "SUM", "sum": "SUM", "min": "MIN", "max": "MAX"}[agg]
    rolled = f"{roll}({p})"
    if agg == "count" and not measure.get("column"):
        # COUNT(*) over no rows is 0, and the grand total of a pivot
        # filtered down to nothing has to keep saying so — SUM over an
        # empty base is NULL.
        rolled = f"COALESCE({rolled}, 0)"
    return [(p, _agg_expr(src, measure, coltypes))], rolled, p


def _rollup_sql(src, measures, coltypes, group_sets, frm, where):
    """Every grouping level in one statement that reads the source once.

    The base CTE groups the filtered rows by every column any level groups
    by, plus each Distinct count's column, keeping _rollup_measure's
    partials; each level is then one UNION ALL arm over the base, tagged
    with its index in `group_sets` and padded to the widest level's key
    count. Querying each level from the source got the same answer with a
    full table scan per level — four for one row field and one column
    field — and that scan, not the grouping, is most of the cost on a wide
    table.

    The base is referenced by every arm, which is what makes SQLite (3.35+)
    materialise it rather than inline it; an older SQLite inlines it into
    each arm, which is the old one-scan-per-level cost, still correct.
    Each arm carries its own LIMIT one past MAX_GROUPS, so `truncated` is
    still per level and one huge level can't crowd out the others."""
    base = []
    for keys in group_sets:
        base.extend(k for k in keys if k not in base)
    base.extend(m["column"] for m in measures
                if m.get("agg") == "count_distinct" and m["column"] not in base)
    alias = {name: f"k{i}" for i, name in enumerate(base)}
    plans = [_rollup_measure(src, m, coltypes, j, alias) for j, m in enumerate(measures)]

    # Never an empty select list: every measure either keeps a partial or,
    # as a Distinct count, adds its column to the keys.
    select = ", ".join([f"{_col(src, k)} AS {alias[k]}" for k in base]
                       + [f"{sql} AS {name}" for parts, _, _ in plans for name, sql in parts])
    base_sql = f"SELECT {select} FROM {frm}{where}"
    if base:
        base_sql += f" GROUP BY {', '.join(str(i + 1) for i in range(len(base)))}"

    width = max(len(keys) for keys in group_sets)
    arms = []
    for level, keys in enumerate(group_sets):
        # The base's own keys in its own order: the base is already grouped
        # that way, so its rows come out in the order that level's GROUP BY
        # would give them — which decides which groups a truncated level keeps.
        same = keys == base
        refs = [alias[k] for k in keys]
        select = ", ".join([str(level)] + refs + ["NULL"] * (width - len(keys))
                           + [same_sql if same else roll for _, roll, same_sql in plans])
        arm = f"SELECT {select} FROM pivot_base"
        if refs and not same:
            arm += f" GROUP BY {', '.join(refs)}"
        arms.append(f"SELECT * FROM ({arm} LIMIT {MAX_GROUPS + 1})")
    return f"WITH pivot_base AS ({base_sql}) " + " UNION ALL ".join(arms), width


def _esc_like(value):
    """`_` and `%` are LIKE wildcards. An analyst typing `SRV_1` into a
    contains filter means the literal underscore — without this, it also
    matches SRVX1, which is the kind of wrong that looks like a wider search
    rather than a bug. Same helper Winnow's own contains/starts filters use.
    """
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _where(src, filters, coltypes, tag_clauses=()):
    """Compiles the Filters area into a WHERE clause plus its parameters.

    Values are bound, never interpolated — the identifiers are the only thing
    that goes into the SQL text, and those are validated against the source's
    real column list first. `tag_clauses` is _tag_clauses' output, ANDed on
    after the field filters; it carries no placeholders, so the parameter
    order _inline relies on is the field filters' alone.
    """
    clauses, params = [], []
    for f in filters or []:
        col, op = f.get("column"), f.get("op", "in")
        if op not in OPERATORS:
            raise ValueError(f"Unknown filter operator {op!r}")
        ident = _col(src, col)
        kind = OPERATORS[op][1]
        if kind == "many":
            vals = [v for v in (f.get("values") or [])]
            if not vals:
                continue  # an empty selection filters nothing, like Excel's "(All)"
            # (blank) in the list is the empty cell, NULL included — what the
            # value list shows it as (it lists a NULL as ''), what a click on
            # a (blank) cell means (/detail), and what '' in a list means to
            # the grid (Store._compile_condition), which "Show these rows in
            # the grid" hands this filter to. As a bare IN it found only ''.
            blank = any(v in (None, "") for v in vals)
            vals = [v for v in vals if v not in (None, "")]
            marks = ",".join("?" * len(vals))
            empty = f"{ident} IS NULL OR {ident} = ''"
            if op == "in":
                clauses.append(f"({ident} IN ({marks}) OR {empty})" if vals and blank
                               else f"{ident} IN ({marks})" if vals else f"({empty})")
            else:
                # A NULL never matches NOT IN, so this spells out that a
                # missing value is *kept* — otherwise excluding one value
                # would silently drop every empty cell too. Unless (blank) is
                # one of the values excluded.
                parts = [f"({ident} NOT IN ({marks}) OR {ident} IS NULL)"] if vals else []
                if blank:
                    parts.append(f"({ident} IS NOT NULL AND {ident} != '')")
                clauses.append(" AND ".join(parts))
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
            else:  # gt / lt — numeric, through the same guarded cast
                try:
                    number = float(val)
                except (TypeError, ValueError):
                    raise ValueError(f"{OPERATORS[op][0]} needs a number, got {val!r}")
                clauses.append(f"{_numeric(src, col)} {'>' if op == 'gt' else '<'} ?")
                params.append(number)
        else:
            clauses.append(f"({ident} IS NULL OR {ident} = '')" if op == "empty"
                           else f"({ident} IS NOT NULL AND {ident} != '')")
    clauses.extend(tag_clauses)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _tagged(src, tag_ids=None):
    """SQL that is true for a row carrying any of `tag_ids` — any tag at all
    when None.

    `row_tags` is keyed (source_id, rid) on the REAL table, so a merge's rows
    are matched through the member each one came from: _scope's union
    carries source_id for exactly this, and a merge's tags live on its
    members (invariant #9). Every id is an int() by the time it gets here,
    which is what lets them sit in the SQL text with no placeholder."""
    only = f" AND tag_id IN ({','.join(str(int(t)) for t in tag_ids)})" if tag_ids else ""

    # rid IN (subquery), the shape Store._compile_where uses, rather than a
    # correlated EXISTS: filtered to a tag, SQLite walks the few tagged rids
    # and seeks each one, instead of probing row_tags once per table row.
    def one(source_id):
        return f"s.rid IN (SELECT rid FROM row_tags WHERE source_id = {int(source_id)}{only})"

    if not src.get("is_merge"):
        return f"({one(src['id'])})"
    # One arm per member, not `(s.source_id, s.rid) IN (SELECT source_id,
    # rid ...)`. Both are right, but SQLite builds no lookup table for a
    # row-value IN, so under NOT — an exclusion, or No tags — it rescans the
    # subquery once per merged row: minutes on two 600k-row members with
    # 50k rows excluded, where this takes under a second.
    return "(" + " OR ".join(f"(s.source_id = {int(m)} AND {one(m)})"
                             for m in src["member_source_ids"]) + ")"


def _tag_clauses(req, src, tags):
    """The tag filter as WHERE clauses: one for the exclusions, one for the
    OR of everything kept. See TAG_ANY for the spelling and why it's that.

    A tag id that isn't in the case is refused rather than compiled. Filtered
    to a deleted tag, the pivot would otherwise answer "no rows" — a
    confident zero, which in triage reads as "none of these happened"."""
    if not tags:
        return []
    if not isinstance(tags, list):
        raise ValueError("tags must be a list")
    known = {int(t["id"]) for t in req.store.list_tags()}
    kept, dropped, any_tag, no_tag = [], [], False, False
    for key in tags:
        if key == TAG_ANY:
            any_tag = True
            continue
        if key == TAG_NONE:
            no_tag = True
            continue
        text = "" if isinstance(key, bool) else str(key)
        excluded = text.startswith("!")
        digits = text[1:] if excluded else text
        if not digits.isdecimal():
            raise ValueError(f"Unknown tag filter {key!r}")
        if int(digits) not in known:
            raise ValueError(f"No tag {int(digits)} in this case — it may have been deleted")
        (dropped if excluded else kept).append(int(digits))
    clauses = []
    if dropped:
        clauses.append(f"NOT {_tagged(src, dropped)}")
    either = ([_tagged(src)] if any_tag else []) + ([f"NOT {_tagged(src)}"] if no_tag else []) \
        + ([_tagged(src, kept)] if kept else [])
    if either:
        clauses.append("(" + " OR ".join(either) + ")")
    return clauses


def _inline(sql, params):
    """run_sql takes no parameters, so the bound values are inlined here —
    through SQLite's own ''-doubling for strings, and as plain literals for
    the numbers _where() has already coerced with float().

    Skipping over quoted spans is the load-bearing part, and neither kind is
    theoretical. Single quotes: the numeric guard embeds NUM_RE's pattern,
    which contains two `?` of its own. Double quotes: a column name is CSV
    header text, and `Elevated?` is a perfectly ordinary header — q() quotes
    it, and a scan that doesn't know that reads the `?` inside the
    identifier as a placeholder. Either way a naive scan runs off the end of
    the parameter list or, worse, silently shifts every later value into the
    wrong slot. Winnow's own _inline_sql_params walks literals for the same
    reason."""
    out, i, k, n = [], 0, 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            # Both quoting styles double the quote to escape it, so one scan
            # handles a string literal and a quoted identifier alike.
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


# ---------------------------------------------------------------- routes


def meta(req):
    """GET /api/plugin/pivot/meta -> the vocabulary the UI builds its pickers
    from, so the two can't drift apart."""
    return {
        "aggregations": [{"id": k, "label": v[0], "needs_column": v[1]} for k, v in AGGREGATIONS.items()],
        "operators": [{"id": k, "label": v[0], "value_kind": v[1]} for k, v in OPERATORS.items()],
        "limits": {"groups": MAX_GROUPS, "group_sets": MAX_GROUP_SETS, "detail_rows": MAX_DETAIL_ROWS},
    }


def aggregate(req):
    """POST /api/plugin/pivot/aggregate
    body: {source_id, values: [{column?, agg}], group_sets: [[col, ...], ...],
           filters: [...], tags?: [...]}
    -> {sets: [{keys: [col, ...], rows: [[k1, .., m1, ..], ...], truncated}], ...}

    One entry per requested grouping level, in the order asked for. `[]` is
    the grand total (a single row of measures, no keys).
    """
    body = req.body or {}
    src = _source(req, body)
    coltypes = {c["name"]: c["type"] for c in src["columns"]}

    measures = body.get("values") or [{"agg": "count"}]
    for m in measures:
        if m.get("column"):
            _check_columns(src, [m["column"]])
        _agg_expr(src, m, coltypes)   # refuses an unknown aggregation, or one missing its column

    group_sets = body.get("group_sets")
    if not isinstance(group_sets, list) or not group_sets:
        raise ValueError("group_sets is required")
    if len(group_sets) > MAX_GROUP_SETS:
        raise ValueError(f"Too many grouping levels ({len(group_sets)} > {MAX_GROUP_SETS})")
    for keys in group_sets:
        if not isinstance(keys, list):
            raise ValueError("each grouping level must be a list of column names")
        _check_columns(src, keys)

    for f in body.get("filters") or []:
        _check_columns(src, [f.get("column")])
    where, params = _where(src, body.get("filters"), coltypes, _tag_clauses(req, src, body.get("tags")))
    frm = _scope(req, src)

    sql, width = _rollup_sql(src, measures, coltypes, group_sets, frm, where)
    # Every arm stops at MAX_GROUPS + 1, so this limit is never what cuts.
    res = req.store.run_sql(_inline(sql, params), limit=len(group_sets) * (MAX_GROUPS + 1))
    by_level = [[] for _ in group_sets]
    for row in res["rows"]:
        keys = group_sets[row[0]]
        by_level[row[0]].append(row[1:1 + len(keys)] + row[1 + width:])
    out = [{"keys": keys, "rows": rows[:MAX_GROUPS], "truncated": len(rows) > MAX_GROUPS}
           for keys, rows in zip(group_sets, by_level)]
    return {"sets": out, "measures": measures, "row_count": src["row_count"]}


def values(req):
    """POST /api/plugin/pivot/values -> distinct values of one column, most
    common first. Backs the Filters area's checkbox list."""
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


def detail(req):
    """POST /api/plugin/pivot/detail -> the rows behind one cell.

    Excel's "Show Details". `cell` is the row/column field values that
    identify the cell; they're turned into equality filters and AND'ed onto
    whatever the Filters area already says — its tag filter included, or a
    cell counted over Suspicious rows would open on every row behind it.
    """
    body = req.body or {}
    src = _source(req, body)
    coltypes = {c["name"]: c["type"] for c in src["columns"]}
    cell = body.get("cell") or []
    for pair in cell:
        _check_columns(src, [pair.get("column")])

    filters = list(body.get("filters") or [])
    for f in filters:
        _check_columns(src, [f.get("column")])
    # A cell keyed on an empty value means "the rows where this column is
    # blank", which is a different question from "equals the empty string" —
    # `empty` covers both spellings the way the rest of Winnow does.
    for pair in cell:
        value = pair.get("value")
        filters.append({"column": pair["column"], "op": "empty"} if value in (None, "")
                       else {"column": pair["column"], "op": "in", "values": [value]})

    where, params = _where(src, filters, coltypes, _tag_clauses(req, src, body.get("tags")))
    cols = [c["name"] for c in src["columns"]]
    # s.rid, not a bare rid: with the sidecar joined, an unqualified rid is
    # ambiguous. Derived columns come from `d`, which is also why they show
    # their real values here rather than their own name repeated.
    select = ", ".join(["s.rid"] + [_col(src, c) for c in cols])
    # On a merge, rid alone collides across members — order deterministically.
    order = "s.source_id, s.rid" if src.get("is_merge") else "s.rid"
    sql = f"SELECT {select} FROM {_scope(req, src)}{where} ORDER BY {order}"
    res = req.store.run_sql(_inline(sql, params), limit=MAX_DETAIL_ROWS)
    return {"columns": ["Line"] + cols, "rows": res["rows"], "truncated": res["truncated"]}
