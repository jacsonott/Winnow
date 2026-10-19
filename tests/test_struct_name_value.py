"""Lists of Name/Value pairs, taken apart by name.

`[{"Name": "UserAgent", "Value": "Mozilla/5.0"}, ...]` — an M365 audit
record's ExtendedProperties, an EvtxECmd payload's `{"@Name", "#text"}`
list, Azure AD's ModifiedProperties — puts its entries in whatever order
the writer chose, so a positional path (`[3].Value`) is a different field
from row to row. These are addressed by a predicate instead,
`ExtendedProperties[Name='UserAgent'].Value`, and that column is called
UserAgent. Also here: the loose grammar load_json falls back to, because
the reported data was Python's repr of such a list, which json.loads
refuses outright.

The browser half (the detail pane offering the same paths and names) is
tests/ui/test_detail_name_value_paths_ui.py.
"""

from __future__ import annotations

import json

import pytest

from winnow import structparse as sp
from winnow import timeparse

PROPS = [
    {"Name": "UserAgent", "Value": "Mozilla/5.0"},
    {"Name": "RequestType", "Value": "OAuth2:Authorize"},
    {"Name": "KeepMeSignedIn", "Value": None},
]


def _discovered(values, kind="json"):
    return {r["path"]: r for r in sp.discover_paths(values, kind)}


def _extract(value, path):
    params = timeparse.validate_params("json_field", {"path": path})
    return timeparse.OPERATIONS["json_field"]["parse"](value, params, {})


# ------------------------------------------------------------- the path

@pytest.mark.parametrize("text, expect", [
    ("$.Props[Name='UserAgent'].Value", ["Props", ("Name", "UserAgent"), "Value"]),
    ('Props[Name="O\'Brien"].Value', ["Props", ("Name", "O'Brien"), "Value"]),
    ("Props[Name='both''\"'].Value", ["Props", ("Name", "both'\""), "Value"]),
    ("$.Data[@Name='LogonType'].#text", ["Data", ("@Name", "LogonType"), "#text"]),
    ("$[ key = 'a.b[0]' ]", [("key", "a.b[0]")]),
])
def test_a_predicate_is_read_as_a_name_to_match(text, expect):
    assert sp.parse_json_path(text) == expect


@pytest.mark.parametrize("name", ["UserAgent", "O'Brien", 'say "hi"', "both'\"", "a]b", "x='y'", " padded "])
def test_a_predicate_round_trips_whatever_the_name_holds(name):
    """The path is written into a column definition and a session file, so
    format(parse(x)) has to come back as x, quotes and brackets included."""
    path = sp.format_json_path(["Props", ("Name", name), "Value"])
    assert sp.parse_json_path(path) == ["Props", ("Name", name), "Value"]
    assert sp.format_json_path(sp.parse_json_path(path)) == path


def test_a_predicate_finds_its_entry_wherever_the_writer_put_it():
    one = json.dumps({"Props": PROPS})
    other = json.dumps({"Props": list(reversed(PROPS))})
    path = "$.Props[Name='UserAgent'].Value"
    assert _extract(one, path) == _extract(other, path) == "Mozilla/5.0"
    # The distinctions the parse-failure count depends on still hold: a
    # null Value is an empty cell, a name that isn't there is a missing one.
    assert _extract(one, "$.Props[Name='KeepMeSignedIn'].Value") == ""
    assert _extract(one, "$.Props[Name='Nope'].Value") is None
    assert _extract(one, "$.Props[Name='UserAgent'].Nope") is None
    # A predicate on something that isn't a list matches nothing.
    assert _extract(json.dumps({"Props": {"Name": "UserAgent"}}), "$.Props[Name='UserAgent']") is None


def test_a_predicate_matches_a_string_name_and_nothing_else():
    """The pane shows a name as text and matches it as text: a 1.0 in one
    row and a "1.0" in the next are not the same name."""
    cell = json.dumps({"P": [{"Name": 1, "Value": "num"}, {"Name": "1", "Value": "str"}]})
    assert _extract(cell, "$.P[Name='1'].Value") == "str"


# -------------------------------------------------------------- discovery

def test_flatten_offers_one_column_per_name_called_by_the_name():
    found = _discovered([json.dumps({"ExtendedProperties": PROPS})])
    assert {p: r["suggested_name"] for p, r in found.items()} == {
        "$.ExtendedProperties[Name='UserAgent'].Value": "UserAgent",
        "$.ExtendedProperties[Name='RequestType'].Value": "RequestType",
        "$.ExtendedProperties[Name='KeepMeSignedIn'].Value": "KeepMeSignedIn",
    }
    # The Name itself isn't a field: it restates the predicate.
    assert not any(p.endswith(".Name") for p in found)


def test_coverage_counts_a_name_across_rows_in_any_order():
    rows = [json.dumps(PROPS), json.dumps(list(reversed(PROPS))), json.dumps(PROPS[:1])]
    found = _discovered(rows)
    assert found["$[Name='UserAgent'].Value"]["count"] == 3
    assert found["$[Name='RequestType'].Value"]["count"] == 2
    # Positionally these rows would have offered $[0].Value three times
    # over, holding a different field in each.
    assert "$[0].Value" not in found


@pytest.mark.parametrize("entries, expect", [
    # EvtxECmd's XML-to-JSON spelling: the name is the @Name attribute and
    # the value is the element's #text.
    ([{"@Name": "SubjectUserSid", "#text": "S-1-5-18"}, {"@Name": "LogonType", "#text": "5"}],
     {"$.EventData.Data[@Name='SubjectUserSid'].#text": "SubjectUserSid",
      "$.EventData.Data[@Name='LogonType'].#text": "LogonType"}),
    # Key/value in lower case (AWS tags, Kubernetes env).
    ([{"key": "Owner", "value": "ops"}, {"key": "Env", "value": "prod"}],
     {"$.EventData.Data[key='Owner'].value": "Owner",
      "$.EventData.Data[key='Env'].value": "Env"}),
    # Two values beside the name (Azure AD ModifiedProperties): the column
    # says which one it holds.
    ([{"Name": "StrongAuth", "OldValue": "[]", "NewValue": "[1]"}],
     {"$.EventData.Data[Name='StrongAuth'].OldValue": "StrongAuth.OldValue",
      "$.EventData.Data[Name='StrongAuth'].NewValue": "StrongAuth.NewValue"}),
    # A Value that is itself a document is walked into.
    ([{"Name": "Client", "Value": {"ip": "10.0.0.5", "port": 443}}],
     {"$.EventData.Data[Name='Client'].Value.ip": "Client.ip",
      "$.EventData.Data[Name='Client'].Value.port": "Client.port"}),
], ids=["evtxecmd", "key-value", "old-new", "nested-value"])
def test_the_shapes_name_value_lists_come_in(entries, expect):
    cell = json.dumps({"EventData": {"Data": entries}})
    found = _discovered([cell])
    assert {p: r["suggested_name"] for p, r in found.items()} == expect
    for path in found:
        assert _extract(cell, path) == found[path]["sample"] or found[path]["sample"] == ""


@pytest.mark.parametrize("entries", [
    # A list of files: a file's name is data, not a field name, so nothing
    # beside it is value-like and the list stays positional.
    [{"name": "a.exe", "size": 3}, {"name": "b.dll", "size": 4}],
    # Two entries with one name: a predicate wouldn't pick out either.
    [{"Name": "X", "Value": 1}, {"Name": "X", "Value": 2}],
    # A name that isn't a string, or isn't there in every entry.
    [{"Name": 7, "Value": "a"}, {"Name": "b", "Value": "b"}],
    [{"Name": "a", "Value": 1}, {"Value": 2}],
    # A name that could not be a column name.
    [{"Name": "", "Value": 1}],
    [{"Name": "line\nbreak", "Value": 1}],
    [{"Name": "x" * 121, "Value": 1}],
    [{"Name": "emoji \U0001F600", "Value": 1}],
], ids=["files", "duplicate-names", "number-name", "missing-name", "empty-name", "control-char",
        "too-long", "astral"])
def test_lists_that_are_not_name_value_pairs_stay_positional(entries):
    found = _discovered([json.dumps({"L": entries})])
    assert not any("[Name=" in p or "[name=" in p for p in found), sorted(found)
    assert any(p.startswith("$.L[0].") for p in found), sorted(found)


def test_every_offered_path_extracts_what_it_was_offered_with():
    """Discovery and extraction walk the same predicate, so a path the
    picker offers, pre-ticked at 100%, must give back its own sample."""
    cell = json.dumps({"P": [{"Name": "O'Brien", "Value": "v1"}, {"Name": 'say "hi"', "Value": "v2"},
                             {"Name": "both'\"", "Value": "v3"}, {"Name": "a.b[0]", "Value": "v4"}]})
    rows = sp.discover_paths([cell], "json")
    assert len(rows) == 4
    for row in rows:
        assert _extract(cell, row["path"]) == row["sample"], row["path"]
    assert sorted(r["suggested_name"] for r in rows) == ["O'Brien", "a.b[0]", "both'\"", 'say "hi"']


@pytest.mark.parametrize("path, name", [
    ("$.Props[Name='UserAgent'].Value", "UserAgent"),
    ("$[Name='UserAgent'].Value", "UserAgent"),
    ("$.Data[@Name='LogonType'].#text", "LogonType"),
    ("$.P[Name='X'].NewValue", "X.NewValue"),
    ("$.P[Name='X'].Value.inner[0]", "X.inner[0]"),
    ("$.P[Name='X']", "X"),
    ("$.P[Name='  X  '].Value", "X"),
    # A blank name has nothing to say, so the path names the column.
    ("$.P[Name=' '].Value", "P[Name=' '].Value"),
    # Positional paths are named as they always were.
    ("$.EventData.Data[0].#text", "EventData.Data[0].#text"),
])
def test_suggest_name_for_name_value_paths(path, name):
    assert sp.suggest_name(path, "json") == name


# --------------------------------------------- what json.loads refuses

@pytest.mark.parametrize("cell, doc", [
    ("[{'Name': 'UserAgent', 'Value': 'Mozilla/5.0'}, {'Name': 'Count', 'Value': None}]",
     [{"Name": "UserAgent", "Value": "Mozilla/5.0"}, {"Name": "Count", "Value": None}]),
    ("{'a': True, 'b': False, 'c': None, 'd': (1, 2), 'e': {3}, 'f': {}}",
     {"a": True, "b": False, "c": None, "d": [1, 2], "e": [3], "f": {}}),
    ('[{"Name": "Ratio", "Value": 1.5},]', [{"Name": "Ratio", "Value": 1.5}]),
    ("[{'s': 'O\\'Brien', 'd': \"it's\", 'u': '\\u00e9\\x41', 'U': '\\U0001F600'}]",
     [{"s": "O'Brien", "d": "it's", "u": "éA", "U": "\U0001F600"}]),
    # An escape neither JSON nor Python knows stays as written: a lax
    # exporter's unescaped Windows path keeps its backslashes.
    ('[{"Value": "C:\\Users\\x\\Desktop"}]', [{"Value": "C:\\Users\\x\\Desktop"}]),
    # A raw string keeps its backslashes; a bytes literal is its text.
    ("[r'\\d+\\n', b'\\x41b']", ["\\d+\\n", "Ab"]),
    # Every digit of a 64-bit-plus integer, and numbers json couldn't read
    # stay as their own text.
    ("[133497123456789012345, 0x1F, 1_000, -0]", [133497123456789012345, "0x1F", "1_000", 0]),
    ("{1: 'a', None: 'b', (1, 2): 'c'}", {"1": "a", "None": "b", "(1, 2)": "c"}),
    # Two escapes that are one character are joined.
    ("['\\ud83d\\ude00']", ["\U0001F600"]),
    # A raw line break is a JSON exporter's mistake json.loads forgives.
    ('{"msg": "line one\nline two"}', {"msg": "line one\nline two"}),
], ids=["python-repr", "python-literals", "trailing-comma", "escapes", "unknown-escape",
        "raw-and-bytes", "numbers", "non-string-keys", "surrogate-pair", "raw-newline"])
def test_load_json_reads_what_json_loads_refuses(cell, doc):
    assert sp.load_json(cell) == doc


def test_nan_is_read_as_json_reads_it():
    out = sp.load_json("[{'Name': 'R', 'Value': NaN}, {'Name': 'S', 'Value': -inf}]")
    assert out[0]["Value"] != out[0]["Value"] and out[1]["Value"] == float("-inf")
    assert _extract("[{'Name': 'R', 'Value': NaN}]", "$[Name='R'].Value") == "nan"


@pytest.mark.parametrize("cell", [
    "[INFO] service started",
    "[1, 2] trailing text",
    "{'a': 1} {'b': 2}",
    "{'a': 1, 2}",
    "{1, 'a': 2}",
    "[1 2]",
    "[,]",
    "[undefined]",
    "['unterminated]",
    "[1abc]",
    "(1, 2)",           # a tuple is never a whole document, as in the pane
    "[" * 300 + "'x'" + "]" * 300,
], ids=["log-prefix", "trailing-text", "two-documents", "dict-then-item", "item-then-dict",
        "no-separator", "empty-item", "unknown-word", "unterminated-string", "number-then-word",
        "bare-tuple", "too-deep"])
def test_load_json_refuses_what_the_pane_would_not_lay_out_as_one_document(cell):
    assert sp.load_json(cell) is None


def test_depth_up_to_the_pane_s_limit_is_read():
    assert sp.load_json("[" * sp.LOOSE_MAX_DEPTH + "1" + "]" * sp.LOOSE_MAX_DEPTH) is not None
    deeper = "[" * (sp.LOOSE_MAX_DEPTH + 1) + "'x'" + "]" * (sp.LOOSE_MAX_DEPTH + 1)
    assert sp.load_json(deeper) is None


def test_a_python_repr_column_is_sniffed_and_flattened_by_name():
    rows = [str(PROPS), str(list(reversed(PROPS)))]
    assert sp.sniff_kind(rows) == "json"
    found = _discovered(rows)
    assert found["$[Name='UserAgent'].Value"]["count"] == 2
    assert found["$[Name='UserAgent'].Value"]["suggested_name"] == "UserAgent"
    for cell in rows:
        assert _extract(cell, "$[Name='RequestType'].Value") == "OAuth2:Authorize"
        assert _extract(cell, "$[Name='KeepMeSignedIn'].Value") == ""


# ---------------------------------------------------------- end to end

def test_flatten_a_python_repr_column_end_to_end(store, write_csv):
    """The reported data, through the store: a CSV whose column holds
    Python's repr of a Name/Value list, in a different order per row, one
    row missing a name. Flatten builds a column per name, called by the
    name, and each row's value lands under its own name."""
    rows = [
        ["Time", "ExtendedProperties"],
        ["2024-01-05 10:00:00", str(PROPS)],
        ["2024-01-05 10:01:00", str([PROPS[1], {"Name": "UserAgent", "Value": "curl/8.0"}, PROPS[2]])],
        ["2024-01-05 10:02:00", str([PROPS[1]])],
    ]
    sid = store.ingest_csv(write_csv(rows), build_fts=False)["id"]
    found = store.detect_struct_paths(sid, "ExtendedProperties")
    assert found["kind"] == "json"
    by_name = {p["suggested_name"]: p for p in found["paths"]}
    assert set(by_name) == {"UserAgent", "RequestType", "KeepMeSignedIn"}
    res = store.add_derived_columns(sid, [
        {"name": n, "input_column": "ExtendedProperties", "op_id": "json_field",
         "params": {"path": by_name[n]["path"]}} for n in ("UserAgent", "RequestType")])
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    src = store.get_source(sid)
    cols = [c["name"] for c in src["columns"]]
    view = store.build_view(sid, {})
    got = [dict(zip(cols, r["cells"])) for r in store.fetch_rows(view["view_id"], 0, 10)["rows"]]
    assert [(g["UserAgent"], g["RequestType"]) for g in got] == [
        ("Mozilla/5.0", "OAuth2:Authorize"),
        ("curl/8.0", "OAuth2:Authorize"),
        (None, "OAuth2:Authorize"),
    ]


def test_a_loose_document_past_its_length_cap_is_not_read():
    """The loose parser is Python, so it reads a million characters at
    most; strict JSON keeps MAX_DOC_BYTES. detail.js stops offering paths
    at the same length (LOOSE_MAX_CHARS there)."""
    item = "{'a': 1}, "
    fits = "[" + item * ((sp.LOOSE_MAX_CHARS - 2) // len(item)) + "]"
    assert len(fits) <= sp.LOOSE_MAX_CHARS and sp.load_json(fits) is not None
    over = "[" + item * (sp.LOOSE_MAX_CHARS // len(item) + 1) + "]"
    assert sp.load_json(over) is None
    assert sp.load_json(over.replace("'", '"').replace(", ]", "]")) is not None
