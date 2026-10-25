"""The detail pane's half of taking a Name/Value list apart by name.

A list like `[{'Name': 'UserAgent', 'Value': 'Mozilla/5.0'}, ...]` is laid
out with each entry addressed by its Name, so right-clicking UserAgent (or
its Value) offers `$[Name='UserAgent'].Value` and "Add as a column" makes a
column called UserAgent — the same column Flatten offers for it. The pane
finds those paths in the browser and the backend extracts them in Python,
so each test here checks the two against each other on the same value:
every path the pane offers extracts what the pane says it holds, every
path Flatten offers can be clicked, and both name the column alike. The
backend half on its own is tests/test_struct_name_value.py.

Through renderDetailContent, like test_detail_loose_documents_ui.py, so no
table is imported for it."""

from __future__ import annotations

import json

import pytest

from winnow import structparse as sp

pytestmark = pytest.mark.ui

RENDER = """(v) => {
  const n = __winnow.renderDetailContent(v);
  const pretty = n.nodeType === 1 && n.matches('pre.detail-pretty');
  const nodes = pretty ? [...n.querySelectorAll('.struct-node')].map((x) => ({
    path: x.dataset.path, value: x.dataset.value === undefined ? null : x.dataset.value,
    text: x.textContent, cls: x.className.replace(' struct-node', ''),
  })) : [];
  const names = {};
  for (const x of nodes) names[x.path] = __winnow.suggestColumnName(x.path, 'json');
  return { pretty, text: n.textContent, nodes, names };
}"""

PROPS = [
    {"Name": "UserAgent", "Value": "Mozilla/5.0"},
    {"Name": "RequestType", "Value": "OAuth2:Authorize"},
    {"Name": "KeepMeSignedIn", "Value": None},
]


def _render(page, value):
    return page.evaluate(RENDER, value)


def test_a_python_name_value_list_is_addressed_by_name(page):
    """The reported shape. The Name stands for the Value beside it, so
    both halves of `'Name': 'UserAgent'` and the Value itself are the one
    field, and its column is called UserAgent."""
    r = _render(page, str(PROPS))
    assert r["pretty"], r
    by_text = {}
    for x in r["nodes"]:
        by_text.setdefault(x["text"], []).append((x["path"], x["value"]))
    ua = "$[Name='UserAgent'].Value"
    assert by_text["'UserAgent'"] == [(ua, "Mozilla/5.0")]
    assert by_text["'Mozilla/5.0'"] == [(ua, "Mozilla/5.0")]
    assert (ua, "Mozilla/5.0") in by_text["'Name'"]
    assert r["names"][ua] == "UserAgent"
    # A Python None is an empty cell, as it is in the column.
    assert by_text["None"] == [("$[Name='KeepMeSignedIn'].Value", "")]
    # Laid out as the cell writes it, as before: only the paths are new.
    assert "'Name': 'UserAgent',\n    'Value': 'Mozilla/5.0'" in r["text"]


CASES = {
    "strict-json": json.dumps({"ExtendedProperties": PROPS, "Id": 7}),
    "python-repr": str({"ExtendedProperties": PROPS, "Id": 7}),
    "evtxecmd": json.dumps({"EventData": {"Data": [
        {"@Name": "SubjectUserSid", "#text": "S-1-5-18"}, {"@Name": "LogonType", "#text": "5"},
        {"@Name": "Empty"}]}}),
    "old-new": json.dumps({"ModifiedProperties": [
        {"Name": "StrongAuth", "OldValue": "[]", "NewValue": "[1]"}]}),
    "key-value": str([{"key": "Owner", "value": "ops"}, {"key": "Env", "value": 3}]),
    "nested-value": str([{"Name": "Client", "Value": {"ip": "10.0.0.5", "port": 443}}]),
    "awkward-names": str([{"Name": "O'Brien", "Value": "v1"}, {"Name": 'say "hi"', "Value": "v2"},
                          {"Name": "both'\"", "Value": "v3"}, {"Name": "a.b[0]", "Value": "v4"},
                          {"Name": "caf\u00e9", "Value": "v5"}]),
    "escapes": "[{'Name': 'Path', 'Value': 'C:\\\\Windows\\\\x'}, {'Name': 'It\\'s', 'Value': '\\u00e9'}]",
    "lax-json": '[{"Name": "Cmd", "Value": "line one\nline two"}, {"Name": "Raw", "Value": "C:\\Users\\x"},]',
    "not-name-value": str({"files": [{"name": "a.exe", "size": 3}, {"name": "b.dll", "size": 4}],
                           "dupes": [{"Name": "X", "Value": 1}, {"Name": "X", "Value": 2}],
                           "numbers": [{"Name": 1, "Value": "a"}, {"Name": "b", "Value": "b"}],
                           "hex": [{"Name": 0x1F, "Value": 1}]}),
    "python-literals": "{'big': 133497123456789012345, 'hex': 0x1F, 't': (1, {'Name': 'T', 'Value': True}),"
                       " 'k': {1: 'one', None: 'none'}, 'n': NaN}",
}


@pytest.mark.parametrize("value", list(CASES.values()), ids=list(CASES))
def test_the_pane_and_the_backend_agree_on_every_path(page, value):
    r = _render(page, value)
    assert r["pretty"], r
    offered = {}
    for x in r["nodes"]:
        offered.setdefault(x["path"], set()).add(x["value"])
    assert offered, "the pane offered no path, so there was nothing to check"
    for path, values in offered.items():
        # Every path the pane offers reads back out of the cell, holding
        # what the node said it holds.
        got = sp._extract_json(value, {"path": path}, {})
        assert got is not None, path
        assert values <= {got, None}, (path, values, got)
        # And "Add as a column" from here names it as Flatten would.
        assert r["names"][path] == sp.suggest_name(path, "json"), path
    # Every field Flatten offers is one the pane lets you click.
    flatten = {row["path"] for row in sp.discover_paths([value], "json")}
    assert flatten <= set(offered), sorted(flatten - set(offered))


def test_name_value_entries_inside_other_text_stay_unaddressed(page):
    """A document inside a log line isn't one the backend can read — it
    reads a cell only when all of it is one document — so the pane lays it
    out, Name/Value list or not, and offers no path on it."""
    r = _render(page, "[INFO] props=" + str(PROPS))
    assert r["pretty"], r
    assert r["nodes"] == []
    assert sp.load_json("[INFO] props=" + str(PROPS)) is None


# The two loose parsers are twins: a whole value the pane makes addressable
# has to be one load_json reads, decoded the same way, or the paths it
# offers name keys and Names the backend never sees. Each of these sits on
# an edge of the grammar; a fuzzed corpus of 20,000 agreed when this was
# written, and these are the shapes it found worth keeping.
TWINS = [
    "[{'Name': 'UserAgent', 'Value': 'Mozilla/5.0'}, {'Name': 'Count', 'Value': None}]",
    "{'a': (1,), 'b': {1, 2}, 'c': {}, 1: [{'x': 1}], None: {'x': r'\\d\\''}}",
    "[{'k': b'\\x41\\u00e9\\U0001F600', 'j': '\\ud83d\\ude00'}]",
    "[{'a': '\\U00110000', 'b': '\\x4', 'c': '\\u12', 'd': '\\q', 'e': '\\/'}]",
    "[{'a': 'line\\\nbreak'}]",
    "[[1, 2], 0x1f, 1_000, 1.5e3, -0, .5, 1., 1j, +1, 133497123456789012345]",
    "{'a': [1, {'b': -inf, 'c': NaN, 'd': Infinity, 'e': -Infinity}]}",
    '[{"Name": "Cmd", "Value": "tab\there"},]',
    "[{'a': 1},,]",
    "[{'a': 1} {'b': 2}]",
    "[{'a': 1, 2}]",
    "[{1, 'a': 2}]",
    "[{'a': }]",
    "[{'a' 1}]",
    "[{'a': 1abc}]",
    "[{'a': undefined}]",
    "[{'a': 'unterminated}]",
    "[{'a': rb'x', 'b': Ub'y'}]",
    "[{'a': bru'x'}]",
    "[{'a': [1, 2)}]",
    "[{'a': 1}] ",
    "  [{'a': 1}]",
    "[" * 255 + "{'a': 1}" + "]" * 255,
    "[" * 256 + "{'a': 1}" + "]" * 256,
]

CANON = """(docs) => docs.map((s) => {
  const canon = (v) => {
    if (typeof v === 'boolean' || v === null || typeof v === 'string') return [typeof v, v];
    if (typeof v === 'number') return ['number', Number.isNaN(v) ? 'nan' : String(v)];
    if (Array.isArray(v)) return ['list', v.map(canon)];
    return ['dict', Object.keys(v).sort().map((k) => [k, canon(v[k])])];
  };
  const d = __winnow.findLooseDocuments(s);
  const blank = /^[ \\t\\r\\n]*$/;
  if (!(d.length === 1 && blank.test(s.slice(0, d[0].start)) && blank.test(s.slice(d[0].end)))) return null;
  // As text: a 255-deep document is too deep to hand back as an object.
  return JSON.stringify(canon(__winnow.looseDecode(d[0].node)));
})"""


def _py_canon(v):
    if isinstance(v, bool) or v is None:
        return ["boolean" if isinstance(v, bool) else "object", v]
    if isinstance(v, str):
        return ["string", v]
    if isinstance(v, (int, float)):
        return None  # compared below as numbers: JS has one number type
    if isinstance(v, list):
        return ["list", [_py_canon(x) for x in v]]
    return ["dict", sorted([k, _py_canon(x)] for k, x in v.items())]


def _same(js, py):
    """js (from CANON) against a Python document, numbers as doubles."""
    if py is None or isinstance(py, (bool, str)):
        return js == _py_canon(py)
    if isinstance(py, (int, float)):
        if js[0] != "number":
            return False
        return js[1] == "nan" if py != py else float(js[1]) == float(py)
    if isinstance(py, list):
        return js[0] == "list" and len(js[1]) == len(py) and all(_same(a, b) for a, b in zip(js[1], py))
    return (js[0] == "dict" and [k for k, _ in js[1]] == sorted(py)
            and all(_same(v, py[k]) for k, v in js[1]))


def test_the_pane_reads_a_whole_value_exactly_when_the_backend_does(page):
    got = [None if js is None else json.loads(js) for js in page.evaluate(CANON, TWINS)]
    for value, js in zip(TWINS, got):
        doc = sp.load_json(value)
        if js is None:
            assert doc is None, value
        else:
            assert doc is not None and _same(js, doc), (value, js, doc)
    # Not vacuous either way.
    assert sum(1 for js in got if js is not None) >= 8
    assert sum(1 for js in got if js is None) >= 8


def test_paths_stop_where_the_backend_stops_reading(page):
    """load_loose reads at most LOOSE_MAX_CHARS characters; past that the
    pane still lays the document out but offers no path on it."""
    for n, addressable in ((sp.LOOSE_MAX_CHARS - 40, True), (sp.LOOSE_MAX_CHARS, False)):
        value = "[{'Name': 'Big', 'Value': '" + "x" * n + "'}]"
        assert (sp.load_json(value) is not None) == addressable, len(value)
        r = page.evaluate("""(v) => {
          const n = __winnow.renderDetailContent(v);
          return [n.nodeType === 1 && n.matches('pre.detail-pretty'), n.nodeType === 1 ? n.querySelectorAll('.struct-node').length : 0];
        }""", value)
        assert r[0], len(value)
        assert (r[1] > 0) == addressable, (len(value), r)
