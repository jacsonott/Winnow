"""The Watchlist's Export… file goes back in through its Import….

Export writes a CSV under a header row (`value,kind,note,hits`, built in
static/js/watchlist.js). Import read every file as a plain one-per-line
list, so the header came back as an indicator called `value`, each kind
as the rest of its line ("filename,,3"), and a value or note holding a
comma was cut at it. `Store.parse_indicator_list` recognises the export's
own header and reads the rest as CSV; these pin that through the route,
and that a plain list — including one whose first indicator is the word
"value" — reads exactly as it did.

tests/ui/test_watchlist_export_roundtrip.py drives the real Export… button
into the real Import… dialog.
"""

from __future__ import annotations

from winnow.store import Store

# Byte for byte what Export… writes for these three indicators: CRLF line
# ends, a cell quoted only when it holds a quote, comma or line break.
EXPORT = (
    "value,kind,note,hits\r\n"
    "evil.exe,filename,,3\r\n"
    '"bad,host.example",domain,"seen in proxy, dns logs",0\r\n'
    '10.0.0.5,ip,"line one\r\nsaid ""hi""",1\r\n'
)

EXPECTED = [
    ("evil.exe", "filename", None),
    ("bad,host.example", "domain", "seen in proxy, dns logs"),
    ("10.0.0.5", "ip", 'line one\r\nsaid "hi"'),
]


def _listed(client):
    return [(i["value"], i["kind"], i["note"]) for i in client.get("/api/watchlist").json()]


def test_an_export_imports_as_the_indicators_it_was_exported_from(client):
    r = client.post("/api/watchlist/import", json={"text": EXPORT, "kind": "other"})
    assert r.status_code == 200
    assert r.json()["added"] == 3
    got = _listed(client)
    # The symptom: an indicator called "value", and kinds like "filename,,3".
    assert "value" not in [v for v, _, _ in got]
    assert sorted(got) == sorted(EXPECTED)


def test_an_export_imported_twice_adds_nothing_the_second_time(client):
    client.post("/api/watchlist/import", json={"text": EXPORT})
    again = client.post("/api/watchlist/import", json={"text": EXPORT}).json()
    assert again["added"] == 0
    assert len(again["indicators"]) == 3


def test_an_empty_kind_in_an_export_takes_the_dialogs_type(client):
    text = "value,kind,note,hits\r\nevil.exe,,,0\r\n"
    client.post("/api/watchlist/import", json={"text": text, "kind": "hash"})
    assert _listed(client) == [("evil.exe", "hash", None)]


def test_comments_and_blanks_before_the_header_are_still_skipped(client):
    text = "# exported for case 42\n\n" + EXPORT
    assert client.post("/api/watchlist/import", json={"text": text}).json()["added"] == 3


def test_a_hand_trimmed_export_still_reads_by_its_header(client):
    """Columns are read by name: an export with `hits` deleted, or the
    columns reordered behind `value`, still lands each cell where it
    belongs."""
    text = "Value,Note,Kind\nevil.exe,from the sandbox,filename\n"
    client.post("/api/watchlist/import", json={"text": text})
    assert _listed(client) == [("evil.exe", "filename", "from the sandbox")]


def test_a_plain_list_starting_with_the_word_value_keeps_it():
    """Only the export's header switches modes. A one-per-line list whose
    first indicator happens to be `value` is a list, and that line is an
    indicator."""
    got = Store.parse_indicator_list("value\nevil.exe\n", "other")
    assert [i["value"] for i in got] == ["value", "evil.exe"]
    # A `value,kind` line is the plain list's own per-line type form, and a
    # line that only LOOKS like a header (an unknown column) stays a line.
    got = Store.parse_indicator_list("value,domain\nevil.exe\n", "other")
    assert [(i["value"], i["kind"]) for i in got] == [("value", "domain"), ("evil.exe", "other")]
    got = Store.parse_indicator_list("value,kind,score\nevil.exe\n", "other")
    assert got[0]["value"] == "value"


def test_a_plain_list_reads_as_it_always_has():
    got = Store.parse_indicator_list("# c\n\nevil.exe\nbad.example,domain\n  10.0.0.5  \n", "ip")
    assert [(i["value"], i["kind"], i["note"]) for i in got] == [
        ("evil.exe", "ip", None), ("bad.example", "domain", None), ("10.0.0.5", "ip", None)]


def test_a_byte_order_mark_does_not_hide_the_header():
    """An export opened and re-saved by a spreadsheet can come back with a
    BOM in front of `value`."""
    got = Store.parse_indicator_list("﻿" + EXPORT, "other")
    assert [i["value"] for i in got] == [v for v, _, _ in EXPECTED]
