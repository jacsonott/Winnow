"""XML the browser's parser rejects, in the detail pane.

Well-formed XML is laid out from the parsed document, every node carrying
the path that addresses it (test_detail_deep_documents_ui.py has an EVTX
record). What real evidence holds is often not well-formed: a record cut
off mid-export, tags that cross, a DOCTYPE the pane refuses to parse. For
those the pane falls back to a heuristic reflow and a regex highlighter
(prettyXml and appendXmlHighlighted in detail.js), written to degrade
rather than throw. Neither had ever run in a test.

What the fallback owes the analyst: one tag to a line, indented by depth;
the tags, attributes and values highlighted; nothing of the value lost or
invented; no path offered, since there is no structure to trust; and the
value shown as text, never as markup, because it is untrusted.

Through renderDetailContent, like the other detail modules, so no table is
imported for it.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui

RENDER = """(v) => {
  const n = __winnow.renderDetailContent(v);
  const pretty = n.nodeType === 1 && n.matches('pre.detail-pretty');
  const spans = (cls) => pretty ? [...n.querySelectorAll('.' + cls)].map((s) => s.textContent) : [];
  return {
    pretty, text: n.textContent,
    tags: spans('xtok-tag'), attrs: spans('xtok-attr'), values: spans('xtok-attrval'),
    paths: pretty ? n.querySelectorAll('[data-path]').length : 0,
    elements: pretty ? [...n.querySelectorAll('*')].map((e) => e.tagName.toLowerCase()) : [],
  };
}"""

# An EVTX record cut off before </EventData></Event>, the way a capped
# export or a carved record ends.
TRUNCATED = (
    '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">'
    '<System><Provider Name="Microsoft-Windows-Security-Auditing"/>'
    '<EventID>4624</EventID><TimeCreated SystemTime="2026-03-14T08:00:00.000Z"/></System>'
    '<EventData><Data Name="TargetUserName">alice</Data><Data Name="IpAddress">10.0.0.5</Data>'
)


def _render(page, value):
    return page.evaluate(RENDER, value)


def _compact(xml):
    return re.sub(r">\s*<", "><", xml).strip()


def _depths(text):
    """(indent, line) for each line of the reflow."""
    return [(len(line) - len(line.lstrip(" ")), line.strip()) for line in text.split("\n")]


def test_a_truncated_record_is_reflowed_and_highlighted_but_not_addressed(page):
    r = _render(page, TRUNCATED)
    assert r["pretty"], "an unparseable record fell through to plain text"
    lines = _depths(r["text"])
    # One tag to a line, every line starting at a tag.
    assert all(line.startswith("<") for _, line in lines), r["text"]
    depth = {line.split(" ")[0].split(">")[0]: d for d, line in lines}
    assert depth["<Event"] < depth["<System"] < depth["<Provider"]
    assert depth["<EventData"] == depth["<System"]
    # Nothing lost or invented: the lines put back together are the value.
    assert "".join(line for _, line in lines) == _compact(TRUNCATED)
    # Highlighted, tag by tag and attribute by attribute.
    assert r["tags"][:4] == ["Event", "System", "Provider", "EventID"]
    assert "Name" in r["attrs"] and '"Microsoft-Windows-Security-Auditing"' in r["values"]
    # And nothing to right-click into a column: it didn't parse.
    assert r["paths"] == 0


def test_tags_that_cross_do_not_break_the_reflow(page):
    """More closing tags than opening ones would take the indent below
    zero; it stops at the margin instead."""
    value = "<a><b>one</a></b></c><d>two</d>"
    r = _render(page, value)
    assert r["pretty"]
    lines = _depths(r["text"])
    assert min(d for d, _ in lines) == 0
    assert "".join(line for _, line in lines) == value
    assert r["paths"] == 0


def test_markup_in_a_fragment_is_shown_as_text_never_run(page):
    """Field values are untrusted forensic data. A tag the highlighter
    doesn't recognise as XML is left as the text it is, not handed to the
    browser to build."""
    value = '<Data Name="Cmd"><img src=x onerror="window.__xmlRan = 1"></Data><Data Name="Next">'
    r = _render(page, value)
    assert r["pretty"]
    assert "img" not in r["elements"]
    assert set(r["elements"]) <= {"span"}
    assert '<img src=x onerror="window.__xmlRan = 1">' in r["text"]


def test_a_doctype_is_shown_unparsed_with_its_entities_left_alone(page):
    """The pane refuses a DOCTYPE, as the backend's extraction does, so it
    never offers a path the backend would decline — and an entity in one is
    shown as written, not expanded."""
    value = '<!DOCTYPE r [<!ENTITY who "expanded">]><r><user>&who;</user></r>'
    r = _render(page, value)
    assert r["pretty"]
    assert "&who;" in r["text"] and "expanded</user>" not in r["text"]
    assert "r" in r["tags"] and "user" in r["tags"]
    assert r["paths"] == 0
