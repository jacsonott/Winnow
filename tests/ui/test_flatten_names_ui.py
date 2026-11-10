"""The flatten picker's default column names are the full JSON path, so
two fields sharing a leaf (target.ip / source.ip) get distinct, telling
names and "select all, flatten" just works — the analyst who wants shorter
names edits them in place."""

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture
def json_table(page, imported, tmp_path):
    f = tmp_path / "flow.csv"
    f.write_text('When,Payload\n'
                 '2026-03-14 08:00:00,"{""target"": {""ip"": ""10.0.0.5"", ""port"": 445}, ""source"": {""ip"": ""10.0.0.9""}}"\n'
                 '2026-03-14 08:00:01,"{""target"": {""ip"": ""10.0.0.6"", ""port"": 22}, ""source"": {""ip"": ""10.0.0.9""}}"\n')
    sid = imported({"path": str(f), "name": "flow.csv", "kind": "csv"})[0]   # finished, not just listed
    page.evaluate("() => __winnow.loadSources()")
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_selector(".row")
    yield sid
    page.evaluate("() => __winnow.closeModal()")
    page.evaluate("(id) => __winnow.api('/api/source/' + id, { method: 'DELETE' }).then(() => __winnow.loadSources())", sid)


def test_defaults_are_full_paths_and_stay_editable(page, json_table):
    page.evaluate("() => __winnow.openFlattenModal('Payload')")
    page.wait_for_selector(".flatten-name input")
    names = page.locator(".flatten-name input").evaluate_all("(els) => els.map((e) => e.value)")
    assert sorted(names) == ["source.ip", "target.ip", "target.port"], names
    # No "ip 2"-style suffixes anywhere, and every name is unique already
    assert len(set(names)) == len(names)
    # Still editable: a shorter name typed in is what the picker keeps
    first = page.locator(".flatten-name input").first
    first.fill("victim")
    assert page.locator(".flatten-name input").first.input_value() == "victim"
