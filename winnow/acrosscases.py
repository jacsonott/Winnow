"""Across cases, read by the same Store code that serves the open case.

An intrusion spans hosts and a Winnow case is one host, so the questions
that matter most need several cases at once: where else did this land,
what happened on these machines in what order, and the one a query can
ask. multicase.py answered them with a reader of its own — a LIKE scan,
its own timeline, its own row shape — and the answers stopped at a count
(#228). This module answers them with the backend the open case already
uses, reaching the other cases as VISITORS: `Store(path, read_only=True)`,
which opens the case file read-only and takes nothing from it (no lock, no
migration, no background index build) while giving every read path the
case itself would run — the Timeline's header-set configuration and
shipped summaries, the Search-all predicate (through the case's own
trigram index when it built one), its tags by name and color.

**One result shape.** Every answer is a list of Timeline lines
(Store.fetch_timeline_rows' shape) with the case each one lives in:
`{case, path, source_id, rid, ts, kind, type_label, source_name, lead,
detail, body, tags: [{id, name, color}], note, matched}`. A sweep hit, a
finding on the cross-case Timeline and a query row that names a row are
the same thing on screen, with the same two ways out: open the row in its
own case, or tag it there.

**Writes go to the case that owns the row, and only when nobody else has
it open** (server.py's /api/across/tag). Tags and notes live in the case
file that owns the row, so that is the only place a tag can mean
anything; a case another Winnow holds is refused by name, never written
under its owner. Everything in this module is a read.
"""

from __future__ import annotations

import contextlib
import heapq
import os
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterator

from winnow.store import (_advanced_clause, _contains_clause, _or_of_positive_terms,
                          _timeline_seconds, _ts_normalize, q)

# The lines a sweep shows per table before "and N more — open the table".
LINES_PER_TABLE = 20
# The most lines the cross-case Timeline holds, over every case. Each case
# offers its first LIMIT + 1, so `truncated` is exact.
TIMELINE_LIMIT = 2000


class NotVisitable(ValueError):
    """The path isn't a case this Winnow can read without opening it."""


@contextlib.contextmanager
def visit(path: str) -> Iterator[Any]:
    """Another case, read-only, through the Store code. Closed on exit,
    leaving nothing beside the case file (sqlite_peek_uri) and no lock
    behind it."""
    from winnow.store import Store, is_winnow_case_file
    if not os.path.isfile(path):
        raise NotVisitable(f"No case file at {path}")
    if not is_winnow_case_file(path):
        raise NotVisitable(f"{os.path.basename(path)} is not a Winnow case")
    try:
        st = Store(path, read_only=True)
    except (ValueError, sqlite3.Error) as e:
        raise NotVisitable(str(e)) from None
    try:
        yield st
    finally:
        st.close()


def case_name(path: str, names: dict[str, str] | None = None) -> str:
    return (names or {}).get(path) or os.path.splitext(os.path.basename(path))[0]


# ----------------------------------------------------------------- lines

def _kind(st, src: dict, cfg: dict) -> str:
    """The Type pill's text, server-side because the page only knows its
    own case's tables: the type the analyst set for this header set, else
    the shipped nickname without the tool's name ("Event logs" for
    EvtxECmd output), else the table's name — the Timeline's own rule."""
    if cfg.get("type_label"):
        return cfg["type_label"]
    match = st._timeline_summary_match(src)
    if match:
        return re.sub(r"\s*\([^)]*\)\s*$", "", match[0]) or match[0]
    return src["name"]


def _tag_defs(st) -> dict[int, dict]:
    return {t["id"]: {"id": t["id"], "name": t["name"], "color": t["color"]} for t in st.list_tags()}


def lines_of(st, configs: dict[int, dict], pairs: list[tuple[int, int]], *, case: str, path: str,
             matched: dict[tuple[int, int], list[str]] | None = None) -> list[dict]:
    """`pairs` of this case as Timeline lines, in Timeline order, each
    carrying its case and its tags by name (tag ids are per case, so an id
    alone means nothing on a page showing several)."""
    if not pairs:
        return []
    view = st.build_timeline_of_rows(configs, pairs)
    rows = st.fetch_timeline_rows(view["view_id"], 0, view["row_count"])["rows"]
    tags = _tag_defs(st)
    srcs = {s["id"]: s for s in st.list_sources()}
    return [_decorate(st, r, srcs, configs, tags, case=case, path=path,
                      matched=(matched or {}).get((r["source_id"], r["rid"]))) for r in rows]


def _decorate(st, r: dict, srcs: dict, configs: dict, tags: dict, *, case: str, path: str,
              matched: list[str] | None = None) -> dict:
    src = srcs.get(r["source_id"]) or {"name": r["source_name"], "columns": []}
    return {
        "case": case, "path": path,
        "source_id": r["source_id"], "rid": r["rid"], "ts": r["ts"],
        "kind": _kind(st, src, configs.get(r["source_id"], {})) if src.get("columns") else r["source_name"],
        "type_label": r["type_label"], "source_name": r["source_name"],
        "lead": r["lead"], "detail": r["detail"], "body": r["body"],
        "tags": [tags[t] for t in r["tags"] if t in tags], "note": r["note"],
        "matched": matched or [],
    }


def restitch_gaps(lines: list[dict]) -> list[dict]:
    """`gap` and `new_day` against the line above in the MERGED list — a
    case's own page computed them against its own previous line, which on
    a page interleaving hosts is the wrong neighbour."""
    prev = None
    for i, ln in enumerate(lines):
        gap, new_day = None, False
        if ln["ts"]:
            new_day = prev is None or prev[:10] != ln["ts"][:10]
            if prev is not None:
                a, b = _timeline_seconds(prev), _timeline_seconds(ln["ts"])
                if a is not None and b is not None:
                    gap = b - a
            prev = ln["ts"]
        ln["gap"], ln["new_day"], ln["pos"] = gap, new_day, i
    return lines


def _line_key(ln: dict) -> tuple:
    return (ln["ts"] is None, ln["ts"] or "", ln["case"], ln["source_id"], ln["rid"])


# ----------------------------------------------------------------- sweep

def _chips(values: list[str]) -> list[dict]:
    return [{"term": v, "connector": "OR", "exclude": False} for v in values]


def _matching_rids(ro, src: dict, terms: list[str], limit: int) -> list[int]:
    cols = [c["name"] for c in src["columns"] if not c.get("derived")]
    fts = q("fts_" + str(src["id"])) if src.get("has_fts") else None
    clause, params = _advanced_clause(cols, _chips(terms), fts)
    if not clause:
        return []
    return [r[0] for r in ro.execute(
        f"SELECT rid FROM {q(src['table_name'])} WHERE {clause} ORDER BY rid LIMIT {int(limit)}", params)]


def _terms_per_rid(ro, src: dict, terms: list[str], rids: list[int]) -> dict[int, list[str]]:
    """Which of `terms` each of these rows holds — the "matched" chips on a
    sweep line. One query per term over just these rows (a rid lookup),
    and only for a table that already matched."""
    cols = [c["name"] for c in src["columns"] if not c.get("derived")]
    fts = q("fts_" + str(src["id"])) if src.get("has_fts") else None
    out: dict[int, list[str]] = {}
    marks = ",".join("?" * len(rids))
    for term in terms:
        clause, params = _contains_clause(cols, term, fts)
        for (rid,) in ro.execute(
                f"SELECT rid FROM {q(src['table_name'])} WHERE rid IN ({marks}) AND {clause}",
                [*rids, *params]):
            out.setdefault(rid, []).append(term)
    return out


def sweep_case(st, values: list[str], configs: dict[int, dict], *, case: str, path: str,
               lines_per_table: int = LINES_PER_TABLE,
               cancelled: Callable[[], bool] = lambda: False) -> list[dict]:
    """One case's answer to "where are these values": Search all's sweep,
    run by this case's own Store (so its trigram index answers when it
    has one), with the first rows of each table that matched as lines.
    Tables in the shape Search all reports: counts, capped, per term."""
    tables = []
    for _scanned, _total, hit, _read in st._iter_search_all_sources("", _chips(values), None):
        if cancelled():
            break
        if hit:
            tables.append(hit)
    srcs = {s["id"]: s for s in st.list_sources()}
    breakdown = _or_of_positive_terms(_chips(values))
    pairs: list[tuple[int, int]] = []
    matched: dict[tuple[int, int], list[str]] = {}
    for t in tables:
        if t.get("error") or cancelled():
            continue
        src = srcs[t["source_id"]]
        with st._reader() as ro:
            rids = _matching_rids(ro, src, values, lines_per_table)
            per = _terms_per_rid(ro, src, values, rids) if breakdown else {r: list(values) for r in rids}
        for rid in rids:
            pairs.append((src["id"], rid))
            matched[(src["id"], rid)] = per.get(rid, [])
        if not t.get("terms") and len(values) == 1:
            t["terms"] = [{"term": values[0], "match_count": t["match_count"], "capped": t["capped"]}]
    lines = lines_of(st, configs, pairs, case=case, path=path, matched=matched)
    by_table: dict[int, list[dict]] = {}
    for ln in lines:
        by_table.setdefault(ln["source_id"], []).append(ln)
    for t in tables:
        t["kind"] = _kind(st, srcs[t["source_id"]], configs.get(t["source_id"], {})) if t["source_id"] in srcs else t["name"]
        t["lines"] = by_table.get(t["source_id"], [])
    tables.sort(key=lambda d: (bool(d.get("error")), -d.get("match_count", 0)))
    return tables


class SweepJobs:
    """One sweep at a time, in the background, polled — the shape Search
    all runs in (Store.start_search_all_job): starting another cancels the
    one running, results land case by case as each finishes, and the
    record says which cases are still to read."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: dict | None = None
        self._seq = 0

    def start(self, paths: list[str], values: list[str], *, names: dict[str, str],
              configs_for: Callable[[Any], dict[int, dict]],
              here: tuple[str, Any] | None = None) -> dict:
        """`here` is (path, store) for the case this Winnow has open: it is
        read through its own Store, not visited, so a sweep sees the rows
        it is tagging right now."""
        with self._lock:
            if self._job is not None:
                self._job["cancelled"] = True
            self._seq += 1
            job = {
                "job_id": self._seq, "values": list(values),
                "cases": [{"case": case_name(p, names), "path": p, "status": "waiting",
                           "tables": [], "error": None, "is_here": bool(here and p == here[0])}
                          for p in paths],
                "done": False, "cancelled": False, "started_at": time.time(),
            }
            self._job = job
        threading.Thread(target=self._run, args=(job, names, configs_for, here), daemon=True,
                         name="winnow-across-sweep").start()
        return self.get(job["job_id"])

    def _run(self, job: dict, names: dict, configs_for, here) -> None:
        def cancelled() -> bool:
            return job["cancelled"]
        try:
            for entry in job["cases"]:
                if cancelled():
                    break
                with self._lock:
                    entry["status"] = "reading"
                try:
                    if here and entry["path"] == here[0]:
                        st = here[1]
                        tables = sweep_case(st, job["values"], configs_for(st), case=entry["case"],
                                            path=entry["path"], cancelled=cancelled)
                    else:
                        with visit(entry["path"]) as st:
                            tables = sweep_case(st, job["values"], configs_for(st), case=entry["case"],
                                                path=entry["path"], cancelled=cancelled)
                    with self._lock:
                        entry["tables"], entry["status"] = tables, "done"
                except (NotVisitable, sqlite3.Error, ValueError, KeyError) as e:
                    # One case mid-copy on a share must not sink thirty.
                    with self._lock:
                        entry["error"], entry["status"] = str(e), "error"
        finally:
            with self._lock:
                job["done"] = True

    def get(self, job_id: int | None = None) -> dict | None:
        with self._lock:
            job = self._job
            if job is None or (job_id is not None and job["job_id"] != job_id):
                return None
            cases = [dict(c, tables=list(c["tables"])) for c in job["cases"]]
            return {
                "job_id": job["job_id"], "values": job["values"], "cases": cases,
                "read": sum(c["status"] in ("done", "error") for c in cases), "total": len(cases),
                "hits": sum(t.get("match_count", 0) for c in cases for t in c["tables"]),
                "done": job["done"], "cancelled": job["cancelled"],
            }

    def cancel(self, job_id: int) -> None:
        with self._lock:
            if self._job is not None and self._job["job_id"] == job_id:
                self._job["cancelled"] = True


# -------------------------------------------------------------- timeline

def _tag_ids_named(st, names: list[str] | None) -> list[int] | None:
    """A tag filter by NAME, since ids are per case. None is every tag;
    a name a case doesn't have matches nothing there."""
    if not names:
        return None
    want = {n.strip().lower() for n in names}
    return [t["id"] for t in st.list_tags() if t["name"].strip().lower() in want] or [-1]


def timeline_case(st, configs: dict[int, dict], *, case: str, path: str, start: str = "",
                  end: str = "", tag_names: list[str] | None = None,
                  limit: int = TIMELINE_LIMIT) -> tuple[list[dict], int]:
    """One case's findings — its tagged rows, as its own Timeline reads
    them — the first `limit + 1` in time order, and how many it has."""
    rng = {"start": start or None, "end": end or None} if (start or end) else None
    built = st.build_timeline(configs, _tag_ids_named(st, tag_names), time_range=rng)
    rows = st.fetch_timeline_rows(built["view_id"], 0, min(built["row_count"], limit + 1))["rows"]
    tags = _tag_defs(st)
    srcs = {s["id"]: s for s in st.list_sources()}
    return [_decorate(st, r, srcs, configs, tags, case=case, path=path) for r in rows], built["row_count"]


def timeline_across(paths: list[str], *, names: dict[str, str],
                    configs_for: Callable[[Any], dict[int, dict]],
                    here: tuple[str, Any] | None = None, start: str = "", end: str = "",
                    tag_names: list[str] | None = None, limit: int = TIMELINE_LIMIT) -> dict:
    """The findings of several cases on one Timeline: each case's tagged
    rows read by its own Timeline code, merged in time order. A bound that
    isn't a timestamp is refused before any case is read, as the case's
    own Timeline refuses it."""
    for b in (start, end):
        if b and _ts_normalize(b) is None:
            raise ValueError(f"Not a timestamp: {b!r}")
    top: list[dict] = []
    per_case: list[dict] = []
    errors: list[dict] = []
    for p in paths:
        name = case_name(p, names)
        try:
            if here and p == here[0]:
                lines, n = timeline_case(here[1], configs_for(here[1]), case=name, path=p, start=start,
                                         end=end, tag_names=tag_names, limit=limit)
            else:
                with visit(p) as st:
                    lines, n = timeline_case(st, configs_for(st), case=name, path=p, start=start,
                                             end=end, tag_names=tag_names, limit=limit)
        except (NotVisitable, sqlite3.Error, KeyError) as e:
            errors.append({"case": name, "path": p, "error": str(e)})
            continue
        per_case.append({"case": name, "path": p, "findings": n})
        top = list(heapq.merge(top, lines, key=_line_key))[:limit + 1]
    return {"lines": restitch_gaps(top[:limit]), "truncated": len(top) > limit,
            "total": sum(c["findings"] for c in per_case), "per_case": per_case, "errors": errors}


# ----------------------------------------------------------------- query

_ONE_TABLE_RE = re.compile(r"\b(c\d+)\s*\.\s*\"?(src_\d+)\"?", re.IGNORECASE)


def query_refs(sql: str, columns: list[str], aliases: dict[str, str]) -> Callable[[list], tuple] | None:
    """How a query's rows name real rows, or None when they don't: a
    `rid` column plus either `case` (an alias, c1…) and `source_id`
    columns, or a query that reads exactly one cN.src_M table — the
    rule the SQL pane follows for its own results, with the case added."""
    low = [c.lower() for c in columns]
    if "rid" not in low:
        return None
    ri = low.index("rid")
    if "case" in low and "source_id" in low:
        ci, si = low.index("case"), low.index("source_id")
        return lambda row: (aliases.get(str(row[ci]).lower()), row[si], row[ri])
    tables = {(a.lower(), t.lower()) for a, t in _ONE_TABLE_RE.findall(sql or "")}
    if len(tables) != 1:
        return None
    (alias, table), = tables
    sid = int(table.split("_", 1)[1])
    path = aliases.get(alias)
    return (lambda row: (path, sid, row[ri])) if path else None


def query_lines(result: dict, sql: str, *, names: dict[str, str],
                configs_for: Callable[[Any], dict[int, dict]],
                here: tuple[str, Any] | None = None) -> list[dict] | None:
    """A cross-case query's rows as lines, when they name rows
    (query_refs), in the order the query returned them."""
    aliases = {c["alias"].lower(): c["path"] for c in result["cases"]}
    ref = query_refs(sql, result["columns"], aliases)
    if ref is None:
        return None
    order: list[tuple] = []
    by_case: dict[str, list[tuple[int, int]]] = {}
    for row in result["rows"]:
        try:
            path, sid, rid = ref(row)
            key = (path, int(sid), int(rid))
        except (TypeError, ValueError):
            continue
        if not path:
            continue
        order.append(key)
        by_case.setdefault(path, []).append(key[1:])
    got: dict[tuple, dict] = {}
    for p, pairs in by_case.items():
        name = case_name(p, names)
        if here and p == here[0]:
            lines = lines_of(here[1], configs_for(here[1]), pairs, case=name, path=p)
        else:
            with visit(p) as st:
                lines = lines_of(st, configs_for(st), pairs, case=name, path=p)
        for ln in lines:
            got[(p, ln["source_id"], ln["rid"])] = ln
    out = [dict(got[k]) for k in order if k in got]
    for i, ln in enumerate(out):
        ln["pos"] = i
    return out


# ------------------------------------------------------------------- row

def row_detail(st, source_id: int, rid: int) -> dict:
    """A line's row in full, and the tags its case offers — what the
    detail strip under a line shows, tag chips included."""
    row = st.timeline_row(source_id, rid)
    tags = _tag_defs(st)
    row["tags"] = [tags[t] for t in row["tags"] if t in tags]
    row["case_tags"] = list(tags.values())
    return row
