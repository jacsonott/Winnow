"""Settings → Imports → "Open new tables when an import finishes": off, a
finished import leaves the analyst where they are (the long-standing
rule); on, the first table of a batch to finish opens and the rest of the
batch land without moving them again. Also that the checkbox is in
Settings and the preference reaches the machine mirror.
"""
import re

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture(autouse=True)
def clean_up(page):
    """Shared server: remove what this module imports, and leave the
    setting off for every other module."""
    before = set(page.evaluate("() => __winnow.S.sources.map((s) => s.id)"))
    yield
    page.wait_for_function("() => !__winnow.ingestJobs.some((j) => j.status === 'running' || j.status === 'queued')", timeout=30_000)
    page.evaluate("""async (before) => {
        __winnow.S.appearance.openNewTables = false; __winnow.saveAppearance();
        await new Promise((r) => setTimeout(r, 150));
        await __winnow.loadSources();
        for (const s of __winnow.S.sources.filter((s) => !before.includes(s.id))) {
          await __winnow.api('/api/source/' + s.id, { method: 'DELETE' }).catch(() => {});
        }
        __winnow.S.sourceId = null; __winnow.S.view = null;
        await __winnow.loadSources();
      }""", sorted(before))


def _csv(tmp_path, name, rows=3):
    p = tmp_path / name
    p.write_text("Host,User\n" + "".join(f"h{i},u{i}\n" for i in range(rows)))
    return p


def _import(page, path, name):
    page.evaluate("""([path, name]) => __winnow.post('/api/ingest/jobs/path',
         { path, name, kind: 'csv' }).then(() => __winnow.startJobsPoll())""", [str(path), name])


def _wait_landed(page, names):
    page.wait_for_function(
        "(names) => names.every((n) => __winnow.S.sources.some((s) => s.name === n)) && !__winnow.ingestJobs.some((j) => j.status === 'running' || j.status === 'queued')",
        arg=names, timeout=30_000)


def test_off_by_default_and_on_opens_the_first_of_a_batch(page, tmp_path):
    assert page.evaluate("() => !!__winnow.S.appearance.openNewTables") is False
    start = page.evaluate("() => __winnow.S.sourceId")
    _import(page, _csv(tmp_path, "quiet.csv"), "quiet.csv")
    _wait_landed(page, ["quiet.csv"])
    assert page.evaluate("() => __winnow.S.sourceId") == start
    # On: a batch of two — the first to finish opens, the second doesn't move us
    page.evaluate("() => { __winnow.S.appearance.openNewTables = true; __winnow.saveAppearance(); }")
    _import(page, _csv(tmp_path, "one.csv", 4), "one.csv")
    _import(page, _csv(tmp_path, "two.csv", 5), "two.csv")
    _wait_landed(page, ["one.csv", "two.csv"])
    page.wait_for_function("() => ['one.csv', 'two.csv'].includes((__winnow.S.sources.find((s) => s.id === __winnow.S.sourceId) || {}).name)")
    landed = page.evaluate("() => __winnow.S.sources.find((s) => s.id === __winnow.S.sourceId).name")
    page.wait_for_timeout(1200)   # a later poll must not re-navigate
    assert page.evaluate("() => __winnow.S.sources.find((s) => s.id === __winnow.S.sourceId).name") == landed
    # The batch is over: the next import opens again
    _import(page, _csv(tmp_path, "three.csv"), "three.csv")
    page.wait_for_function("() => (__winnow.S.sources.find((s) => s.id === __winnow.S.sourceId) || {}).name === 'three.csv'", timeout=30_000)


def test_the_checkbox_lives_under_imports_and_persists(page):
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    # By the section's own title: "imports" also appears in other sections' help text
    sec = page.locator("#modalBody .settings-section").filter(has=page.locator(".settings-section-title", has_text=re.compile(r"^Imports$")))
    sec.locator(".settings-section-head").click()   # sections start collapsed
    cb = sec.locator("label.check-row", has_text="Open new tables").locator("input")
    cb.wait_for(state="visible")
    assert not cb.is_checked()
    cb.check()
    page.wait_for_function("() => __winnow.S.appearance.openNewTables === true")
    assert page.evaluate("() => JSON.parse(localStorage.getItem('winnow.appearance')).openNewTables") is True
    # …and it reaches the machine mirror (the key is whitelisted there)
    machine = None
    for _ in range(20):
        machine = page.evaluate("() => __winnow.api('/api/settings/app')")
        if (machine.get("appearance") or {}).get("openNewTables") is True:
            break
        page.wait_for_timeout(200)
    assert (machine.get("appearance") or {}).get("openNewTables") is True, machine
    page.evaluate("() => __winnow.closeModal()")
