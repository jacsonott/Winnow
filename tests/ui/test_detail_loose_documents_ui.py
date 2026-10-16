"""Values JSON.parse refuses but that are still documents, laid out in the
detail pane instead of shown as one unbroken line: Python's repr of a list
of dicts (the reported case, a list of {'Name': ..., 'Value': ...}), JSON a
lax exporter wrote, and a document inside other text such as a log line's
`[INFO] {...}`. Only a strict whole-value parse was ever tried before.

These go through renderDetailContent, the function showDetail turns every
field into, in the real browser, rather than through a table of their own:
what changed is what one value renders as, and a table imported for it
would be one more thing every later module in the shard has to look past."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

RENDER = """(v) => {
  const n = __winnow.renderDetailContent(v);
  const pretty = n.nodeType === 1 && n.matches('pre.detail-pretty');
  return {
    pretty,
    text: n.textContent,
    paths: pretty ? [...n.querySelectorAll('.struct-node')].map((x) => x.dataset.path) : [],
    tokens: pretty ? [...n.querySelectorAll('span')].map((x) => [x.className, x.textContent]) : [],
  };
}"""


def _render(page, value):
    return page.evaluate(RENDER, value)


def test_a_python_list_of_name_value_dicts_is_laid_out(page):
    r = _render(page, "[{'Name': 'UserAgent', 'Value': 'Mozilla/5.0'}, "
                      "{'Name': 'Count', 'Value': None}]")
    assert r["pretty"], r
    # Every token as the cell writes it: Python's quotes and None stay, so
    # text selected here still matches what a contains-filter looks for.
    assert r["text"] == (
        "[\n"
        "  {\n    'Name': 'UserAgent',\n    'Value': 'Mozilla/5.0'\n  },\n"
        "  {\n    'Name': 'Count',\n    'Value': None\n  }\n"
        "]")
    assert ["jtok-key", "'Name'"] in r["tokens"]
    assert ["jtok-str", "'UserAgent'"] in r["tokens"]
    assert ["jtok-null", "None"] in r["tokens"]
    # Not addressable: json_field reads only a cell that is wholly one strict
    # JSON document, so "Add as a column" from here would come back empty.
    assert r["paths"] == []


@pytest.mark.parametrize("value, shown", [
    ('[{"Name":"Cmd","Value":"line one\nline two"}]',
     '[\n  {\n    "Name": "Cmd",\n    "Value": "line one\nline two"\n  }\n]'),
    ('[{"Name":"Path","Value":"C:\\Windows\\System32\\cmd.exe"}]',
     '[\n  {\n    "Name": "Path",\n    "Value": "C:\\Windows\\System32\\cmd.exe"\n  }\n]'),
    ('[{"Name":"Ratio","Value":NaN},]',
     '[\n  {\n    "Name": "Ratio",\n    "Value": NaN\n  }\n]'),
    # A FILETIME past 2^53 keeps its digits: nothing is decoded to a Number.
    ("[{'Name': 'FileTime', 'Value': 133497123456789012345}]",
     "[\n  {\n    'Name': 'FileTime',\n    'Value': 133497123456789012345\n  }\n]"),
], ids=["raw-newline", "unescaped-backslash", "nan-and-trailing-comma", "big-integer"])
def test_json_a_lax_exporter_wrote_is_laid_out(page, value, shown):
    r = _render(page, value)
    assert r["pretty"], r
    assert r["text"] == shown


@pytest.mark.parametrize("value, shown", [
    ('[INFO] {"event":"logon","user":"bob"}',
     '[INFO] {\n  "event": "logon",\n  "user": "bob"\n}'),
    ('[ctx {"a":1,"b":[1,2]}]',
     '[ctx {\n  "a": 1,\n  "b": [\n    1,\n    2\n  ]\n}]'),
    ('{"a":1} {"b":2}',
     '{\n  "a": 1\n} {\n  "b": 2\n}'),
    # A bracket inside a string closes nothing, and an apostrophe in the
    # text around the document starts nothing.
    ("[user's session] {'msg': 'a } and ] inside'} don't",
     "[user's session] {\n  'msg': 'a } and ] inside'\n} don't"),
], ids=["log-prefix", "wrapper", "two-documents", "brackets-and-apostrophes"])
def test_a_document_inside_other_text_is_laid_out_where_it_sits(page, value, shown):
    r = _render(page, value)
    assert r["pretty"], r
    assert r["text"] == shown
    assert r["paths"] == []


def test_a_strict_json_list_stays_an_addressable_document(page):
    r = _render(page, '[{"Name":"UserAgent","Value":"x"},{"Name":"Count","Value":2}]')
    assert r["pretty"], r
    assert "$[0].Name" in r["paths"] and "$[1].Value" in r["paths"], r["paths"]


@pytest.mark.parametrize("value", [
    # Brackets with nothing worth laying out inside: `[0]` is a list, `{}`
    # an object, and neither is worth five lines.
    "hostd[2099123]: [Originator@6876 sub=Vimsvc opID=x] items[0] started",
    "[INFO] service started {}",
    "(see [1], [2])",
    "['S-1-5-18', 'SYSTEM']",
], ids=["log-brackets", "empty-object", "citations", "list-of-strings"])
def test_brackets_that_hold_no_document_stay_text(page, value):
    r = _render(page, value)
    assert not r["pretty"], r
    assert r["text"] == value


@pytest.mark.parametrize("value", ["[" * 300_000, "[x]" * 100_000, "[ " * 500_000],
                         ids=["unmatched-brackets", "bracket-pairs", "spaced-brackets"])
def test_a_hostile_value_still_renders_as_text(page, value):
    """Every `[` is a candidate and a failed one is retried a character
    later, so these are the quadratic shapes; the scan budget bounds them."""
    assert page.evaluate(
        "(v) => { const n = __winnow.renderDetailContent(v); return n.nodeType === 3 && n.data === v; }",
        value)
