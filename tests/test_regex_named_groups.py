"""Named capture groups, one column each.

A regex derive could keep one thing out of a match: the first group, or a
group you addressed by number. But one pattern over one column is usually
several columns of intent — an IIS line is a method, a path, a status and
a time — so getting all four meant writing the same regex four times,
each with a different number, and four full scans of the table.

A definition can now name the group it keeps. The name is the point: a
pattern gets edited (a group added in the middle, an alternation widened)
and every numbered definition after the edit quietly starts keeping a
different field, where a named one does not move.
"""

from __future__ import annotations

import time

import pytest

from winnow import structparse
from winnow.store import Store

# One access-log line's worth of fields, named.
IIS = r'^(?P<ip>\S+) (?P<method>[A-Z]+) (?P<path>\S+) (?P<status>\d{3})$'


@pytest.fixture
def case(tmp_path):
    p = tmp_path / "iis.csv"
    rows = [
        "10.0.0.1 GET /index.html 200",
        "10.0.0.2 POST /api/login 401",
        "10.0.0.3 GET /admin 403",
        "not a log line at all",
    ]
    p.write_text("Line\n" + "\n".join(rows) + "\n", encoding="utf-8")
    store = Store(str(tmp_path / "case.db"))
    try:
        yield store, store.ingest_csv(str(p))
    finally:
        store.close()


def _values(store, src, column):
    v = store.build_view(src["id"], {"filters": [], "sort": []})
    rows = store.fetch_rows(v["view_id"], 0, 10)["rows"]
    cols = [c["name"] for c in store.get_source(src["id"])["columns"]]
    i = cols.index(column)
    return [r["cells"][i] for r in rows]


# ------------------------------------------------------------- the op itself


def test_a_named_group_is_what_the_column_keeps():
    state: dict = {}
    params = {"pattern": IIS, "group_name": "path"}
    assert structparse._extract_regex("10.0.0.1 GET /index.html 200", params, state) == "/index.html"
    # …and a line the pattern does not match is a NULL, not an error.
    assert structparse._extract_regex("nope", params, state) is None


def test_the_name_wins_over_the_number():
    """Both set is not a conflict to refuse — the name is the more
    specific statement, and refusing would make the UI's own defaults
    (group 0) impossible to send alongside one."""
    state: dict = {}
    params = {"pattern": IIS, "group_name": "status", "group": 1}
    assert structparse._extract_regex("10.0.0.1 GET /a 404", params, state) == "404"


def test_a_name_the_pattern_does_not_declare_is_refused_up_front():
    with pytest.raises(ValueError) as e:
        structparse._validate_regex_params({"pattern": IIS, "group_name": "nope"})
    # …and says what the pattern does name, so the fix is in the message.
    assert "ip, method, path, status" in str(e.value)


def test_the_names_come_back_in_the_order_they_open():
    """Alphabetical would be wrong: the columns are created in this order,
    and a path/method/status pattern read back as method/path/status is a
    definition you have to decode rather than read."""
    assert structparse.regex_group_names(IIS) == ["ip", "method", "path", "status"]
    assert structparse.regex_group_names(r"no groups here") == []


def test_an_uncompilable_pattern_is_a_value_error_not_a_crash():
    with pytest.raises(ValueError):
        structparse.regex_group_names(r"(?P<a>unclosed")


# ------------------------------------------------------- discovery and batch


def test_discovery_reports_each_group_with_what_it_would_pull(case):
    store, src = case
    got = store.preview_regex_groups(src["id"], "Line", IIS)
    assert [g["name"] for g in got["groups"]] == ["ip", "method", "path", "status"]
    assert got["sampled"] == 4 and got["matched"] == 3
    by = {g["name"]: g["samples"] for g in got["groups"]}
    assert by["method"] == ["GET", "POST", "GET"]
    assert by["path"][0] == "/index.html"


def test_discovery_on_a_column_that_is_not_there_is_a_keyerror(case):
    store, src = case
    with pytest.raises(KeyError):
        store.preview_regex_groups(src["id"], "Nope", IIS)


def test_one_pass_makes_a_column_per_group(case):
    """The whole point: four columns, one scan, and each one an ordinary
    derived column afterwards."""
    store, src = case
    res = store.add_derived_columns(src["id"], [
        {"name": n.title(), "input_column": "Line", "op_id": "regex_extract",
         "params": {"pattern": IIS, "group_name": n}}
        for n in ("ip", "method", "path", "status")
    ])
    assert res
    # The backfill runs as a job; wait for the definitions to report ready.
    deadline = time.time() + 20
    while time.time() < deadline:
        defs = store.list_derived_columns(src["id"])
        if defs and all(d["status"] == "ready" for d in defs):
            break
        time.sleep(0.05)
    defs = store.list_derived_columns(src["id"])
    assert [d["name"] for d in defs] == ["Ip", "Method", "Path", "Status"]
    assert all(d["status"] == "ready" for d in defs), defs

    assert _values(store, src, "Method") == ["GET", "POST", "GET", None]
    assert _values(store, src, "Status") == ["200", "401", "403", None]


def test_the_batch_is_all_or_nothing_on_a_bad_group_name(case):
    """A name the pattern does not declare fails validation before any
    column exists — the same all-or-nothing the flatten picker relies on."""
    store, src = case
    with pytest.raises(ValueError):
        store.add_derived_columns(src["id"], [
            {"name": "Ip", "input_column": "Line", "op_id": "regex_extract",
             "params": {"pattern": IIS, "group_name": "ip"}},
            {"name": "Nope", "input_column": "Line", "op_id": "regex_extract",
             "params": {"pattern": IIS, "group_name": "nope"}},
        ])
    assert store.list_derived_columns(src["id"]) == []


def test_the_route_answers_400_for_a_pattern_that_will_not_compile(client, store, tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("Line\nx\n", encoding="utf-8")
    src = store.ingest_csv(str(p))
    r = client.post("/api/derived/regex_groups",
                    json={"source_id": src["id"], "column": "Line", "pattern": "(?P<a>unclosed"})
    assert r.status_code == 400
