"""structparse.py: JSON/XML path syntax, extraction, and field discovery.

Pure functions, tested as such (same shape as test_timeparse.py) — the
store-level integration is in test_derived_extract.py.
"""

from __future__ import annotations

import json

import pytest

import structparse as sp

EVTX = (
    '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">'
    '<System><Provider Name="Microsoft-Windows-Security-Auditing"/>'
    "<EventID>4624</EventID></System>"
    '<EventData><Data Name="TargetUserName">jacson</Data>'
    '<Data Name="LogonType">3</Data>'
    '<Data Name="IpAddress">10.0.0.5</Data></EventData></Event>'
)

DOC = {
    "user": {"name": "jacson", "id": 7, "admin": True},
    "src": {"ip": "10.0.0.5"},
    "tags": ["a", "b"],
    "empty": None,
    "sessions": [{"id": "s1"}, {"id": "s2"}],
}


# ------------------------------------------------------------- JSON paths

@pytest.mark.parametrize("text,expect", [
    ("$.user.name", ["user", "name"]),
    ("user.name", ["user", "name"]),
    ("items[0].id", ["items", 0, "id"]),
    ('["odd.key"].v', ["odd.key", "v"]),
    ("['odd.key']", ["odd.key"]),
    ("$", None),  # bare root is not a field
])
def test_parse_json_path(text, expect):
    if expect is None:
        with pytest.raises(ValueError):
            sp.parse_json_path(text)
    else:
        assert sp.parse_json_path(text) == expect


@pytest.mark.parametrize("text", ["$.a.b", "$.a[0].b", '$["a.b"].c', '$["a\\"q"]'])
def test_json_path_round_trips(text):
    """Paths are written into column definitions and session files, so
    format(parse(x)) has to be re-parseable — including for keys dot
    notation can't express."""
    once = sp.format_json_path(sp.parse_json_path(text))
    assert sp.format_json_path(sp.parse_json_path(once)) == once


def x(path, value=DOC):
    return sp._extract_json(json.dumps(value), {"path": path}, {})


def test_json_extraction():
    assert x("$.user.name") == "jacson"
    assert x("$.user.id") == "7"
    assert x("$.user.admin") == "true"
    assert x("$.tags") == '["a", "b"]'  # a whole array lands as one text cell
    assert x("$.sessions[1].id") == "s2"


def test_missing_field_is_none_but_json_null_is_empty():
    """The distinction the parse-failure count depends on: a field that
    isn't there is a different answer from a field that is there and
    null."""
    assert x("$.nope") is None
    assert x("$.user.nope") is None
    assert x("$.empty") == ""


def test_non_json_input_is_none():
    assert sp._extract_json("not json", {"path": "$.a"}, {}) is None
    assert sp._extract_json("", {"path": "$.a"}, {}) is None
    assert sp._extract_json(None, {"path": "$.a"}, {}) is None
    assert sp._extract_json("{broken", {"path": "$.a"}, {}) is None


def test_wrong_shape_at_a_step_is_none():
    assert x("$.user[0]") is None       # indexing an object
    assert x("$.tags.name") is None     # keying an array


def test_json_leaf_paths_walks_objects_and_object_arrays():
    paths = {sp.format_json_path(p) for p, _ in sp.json_leaf_paths(DOC)}
    assert "$.user.name" in paths
    assert "$.sessions[0].id" in paths   # array of objects is indexed into
    assert "$.tags" in paths             # array of scalars stays one leaf
    assert "$.tags[0]" not in paths


# -------------------------------------------------------------- XML paths

def test_xml_extraction_by_element_and_attribute():
    assert sp._extract_xml(EVTX, {"path": "Event/System/EventID"}, {}) == "4624"
    assert sp._extract_xml(EVTX, {"path": "Event/System/Provider@Name"}, {}) \
        == "Microsoft-Windows-Security-Auditing"
    assert sp._extract_xml(EVTX, {"path": "Event/EventData/Data[1]"}, {}) == "3"


def test_xml_extraction_by_attribute_predicate():
    """The EVTX shape: addressing repeated <Data> by its Name, not its
    position, because position isn't stable across event IDs."""
    p = "Event/EventData/Data[@Name='IpAddress']"
    assert sp._extract_xml(EVTX, {"path": p}, {}) == "10.0.0.5"
    assert sp._extract_xml(EVTX, {"path": "Event/EventData/Data[@Name='Nope']"}, {}) is None


def test_xml_namespaces_are_matched_on_local_name():
    # EVTX above is namespaced; every assertion in this file writes bare
    # names, which is the whole point.
    assert sp._extract_xml(EVTX, {"path": "Event/System/EventID"}, {}) == "4624"


def test_xml_path_parsing_handles_brackets_and_slashes():
    steps, attr = sp.parse_xml_path("a/b[@N='x/y']/c")
    assert steps == [("a", 0), ("b", ("N", "x/y")), ("c", 0)]
    assert attr is None
    # An @ inside a predicate is not the attribute selector.
    steps, attr = sp.parse_xml_path("Data[@Name='LogonType']")
    assert attr is None
    steps, attr = sp.parse_xml_path("Data[@Name='LogonType']@Other")
    assert attr == "Other"


def test_xml_path_round_trips():
    for text in ["Event/System/EventID", "Data[2]", "Data[@Name='X']", "Provider@Name"]:
        steps, attr = sp.parse_xml_path(text)
        assert sp.format_xml_path(steps, attr) == text


def test_xml_fragment_with_several_roots():
    """A cell holding a slice of a document, not a whole one."""
    assert sp._extract_xml("<a>1</a><b>2</b>", {"path": "b"}, {}) == "2"


def test_doctype_is_refused():
    """ElementTree expands internal entities, so a document that declares
    any is not parsed at all — see the module docstring."""
    bomb = ('<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
            "<lolz>&lol2;</lolz>")
    assert sp.load_xml(bomb) is None
    assert sp._extract_xml(bomb, {"path": "lolz"}, {}) is None


def test_oversized_documents_are_refused():
    big = "{" + '"a":"' + "x" * (sp.MAX_DOC_BYTES + 10) + '"}'
    assert sp.load_json(big) is None


def test_malformed_xml_is_none():
    assert sp._extract_xml("<a><b></a>", {"path": "a/b"}, {}) is None
    assert sp._extract_xml("not xml", {"path": "a"}, {}) is None


# -------------------------------------------------------------- discovery

def test_sniff_kind():
    assert sp.sniff_kind([json.dumps(DOC)]) == "json"
    assert sp.sniff_kind([EVTX]) == "xml"
    assert sp.sniff_kind(["plain", "text"]) is None
    assert sp.sniff_kind([]) is None
    # Majority wins — one stray blob doesn't make a text column structured.
    assert sp.sniff_kind(["plain", "text", json.dumps(DOC)]) is None


def test_discover_paths_reports_coverage():
    rows = [json.dumps({"a": 1, "b": 2}), json.dumps({"a": 3})]
    found = {r["path"]: r for r in sp.discover_paths(rows, "json")}
    assert found["$.a"]["count"] == 2 and found["$.a"]["coverage"] == 1.0
    assert found["$.b"]["count"] == 1 and found["$.b"]["coverage"] == 0.5
    # Best-covered first, so the columns worth making are at the top.
    assert sp.discover_paths(rows, "json")[0]["path"] == "$.a"


def test_discover_paths_names_evtx_data_elements_after_their_name_attribute():
    found = {r["path"]: r["suggested_name"] for r in sp.discover_paths([EVTX], "xml")}
    assert found["Event/EventData/Data[@Name='TargetUserName']"] == "TargetUserName"
    assert found["Event/System/EventID"] == "EventID"
    # The attribute that selects the element restates the predicate.
    assert "Event/EventData/Data[@Name='TargetUserName']@Name" not in found


def test_discovered_paths_all_extract():
    """Whatever discovery offers has to be addressable — a path in the
    picker that extracts nothing would build a column of NULLs."""
    for row in sp.discover_paths([EVTX], "xml"):
        assert sp._extract_xml(EVTX, {"path": row["path"]}, {}) is not None, row["path"]
    doc = json.dumps(DOC)
    for row in sp.discover_paths([doc], "json"):
        assert sp._extract_json(doc, {"path": row["path"]}, {}) is not None, row["path"]


def test_registered_ops_are_hidden_from_timestamp_detection():
    import timeparse

    ops = {o["id"]: o for o in timeparse.list_ops()}
    assert ops["json_field"]["family"] == "extract"
    assert ops["xml_field"]["derived_kind"] == "text"
    # A JSON column must never be offered as a timestamp format.
    assert not any(r["op_id"] in ("json_field", "xml_field")
                   for r in timeparse.detect([json.dumps(DOC)]))


def test_bad_paths_are_rejected_at_validation_time():
    import timeparse

    with pytest.raises(ValueError):
        timeparse.validate_params("json_field", {"path": "a..b["})
    with pytest.raises(ValueError):
        timeparse.validate_params("xml_field", {"path": "a[[["})
    assert timeparse.validate_params("json_field", {"path": "$.a.b"})["path"] == "$.a.b"


def test_present_but_always_empty_fields_are_distinguished():
    """`<TimeCreated SystemTime="..."/>` is present in every row and empty
    in every row — the picker needs to tell that apart from a field that
    carries a value, or ticking "100% coverage" builds a column of blanks."""
    rows = ['<E><TimeCreated SystemTime="2024-01-05"/><EventID>4624</EventID></E>'] * 3
    found = {r["path"]: r for r in sp.discover_paths(rows, "xml")}
    assert found["E/TimeCreated"]["count"] == 3
    assert found["E/TimeCreated"]["nonempty"] == 0
    assert found["E/TimeCreated@SystemTime"]["nonempty"] == 3
    assert found["E/EventID"]["nonempty"] == 3
    # Fields with something in them sort first.
    assert sp.discover_paths(rows, "xml")[-1]["path"] == "E/TimeCreated"
