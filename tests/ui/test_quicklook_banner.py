"""The quick-look banner: visible in a temp case, absent in a real one,
and Save-as promotes the case while the analyst watches."""

from __future__ import annotations

import pytest

from spawn import running_server

pytestmark = pytest.mark.ui


def test_no_banner_on_a_real_case(page):
    assert page.locator("#tempBanner").get_attribute("hidden") is not None


@pytest.fixture
def quicklook_server(tmp_path):
    """A real --assoc launch, since temp-ness is a fact about the path and
    the shared fixture's case is real. Its cases dir is tmp_path/cases (see
    tests/spawn.py), which is where Save as a case should propose."""
    dropped = tmp_path / "dropped.csv"
    dropped.write_text("Host,User\nh1,alice\nh2,bob\n", encoding="utf-8")
    with running_server(tmp_path, "--assoc", dropped, "--no-fts", launcher=True, cwd=tmp_path,
                        env={"WINNOW_IDLE_EXIT_S": "600", "WINNOW_NEVER_CONNECTED_EXIT_S": "600"}) as srv:
        yield srv.base


def _quicklook(own_page, base):
    return own_page(base=base + "/", viewport=(1200, 700), wait_until="load",
                    ready="#tempBanner:not([hidden])", ready_timeout=20_000)


def test_banner_shows_and_save_as_promotes(own_page, quicklook_server, tmp_path):
    with _quicklook(own_page, quicklook_server) as pg:
        assert "temporary" in pg.locator("#tempBanner").inner_text()

        # Saving opens + New case's own dialog, not a bare name prompt
        # (reported: it only asked for a name, so a quick look could not
        # be given a group, a path or a profile like any other case).
        pg.locator("#tempSaveBtn").click()
        pg.wait_for_selector("#modal:not([hidden])")
        assert pg.locator("#modalTitle").text_content() == "Save as a case"
        labels = pg.locator("#modalBody .nc-case .dlg-field > label").all_inner_texts()
        assert labels == ["Name", "Group", "Case file path"], labels
        pg.wait_for_selector("#modalBody .side-rail-item[data-section=none][aria-selected=true]")
        assert pg.locator(".confirm-overlay").count() == 0

        inputs = pg.locator("#modalBody input")
        inputs.nth(0).fill("Promoted case")
        inputs.nth(1).fill("ACME IR")
        # The path follows the name into the folder the quick look already
        # lives under, so the default save is a rename, not a copy.
        path = inputs.nth(2).input_value()
        assert path.startswith(str(tmp_path / "cases")), path
        assert path.endswith("promoted-case.db-winnow"), path
        save = pg.locator("#modal .nc-go")
        assert save.text_content() == "Save as \u201cPromoted case\u201d"
        save.click()

        pg.wait_for_selector("#tempBanner[hidden]", state="attached", timeout=15_000)
        cur = pg.evaluate("""() => fetch('/api/case/current',
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
        assert cur["temp"] is False
        assert cur["name"] == "Promoted case"
        assert cur["path"] == path
        rec = next(c for c in pg.evaluate("""() => fetch('/api/cases',
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
                   if c["path"] == path)
        assert rec["group"] == "ACME IR"


def test_cancelling_save_from_the_leave_guard_goes_back_to_it(own_page, quicklook_server):
    """The guard's Save opens the save dialog in its place (one modal at a
    time); Cancel there returns to the guard's question rather than
    dropping the analyst back into the quick look with no dialog."""
    with _quicklook(own_page, quicklook_server) as pg:
        pg.locator("#btnHome").click()
        pg.wait_for_selector("#modal:not([hidden])")
        pg.locator("#modal button", has_text="Save as a case").click()
        pg.wait_for_function(
            "() => document.getElementById('modalTitle').textContent === 'Save as a case'")
        pg.locator("#modalBody .btn", has_text="Cancel").click()
        pg.wait_for_function(
            "() => /isn.t saved/.test(document.getElementById('modalTitle').textContent)")
        assert not pg.evaluate("() => document.getElementById('modal').hidden")
        assert pg.evaluate("() => document.getElementById('home').hidden")


def test_home_navigation_is_guarded_in_a_quicklook(own_page, quicklook_server, tmp_path):
    """A quick look never appears in the case list, so the home screen
    has no way back to it — clicking the brand used to strand the case
    (reported). The guard offers the three real exits, and Stay works."""
    with _quicklook(own_page, quicklook_server) as pg:

        pg.locator("#btnHome").click()
        pg.wait_for_selector("#modal:not([hidden])")
        assert "isn't saved" in pg.locator("#modalTitle").inner_text().lower().replace("’", "'")
        assert pg.evaluate("() => document.getElementById('home').hidden"), \
            "the guard must hold navigation, not race it"

        pg.locator("#modal button", has_text="Stay here").click()
        pg.wait_for_selector("#modal[hidden]", state="attached")
        assert pg.evaluate("() => document.getElementById('home').hidden")
        assert not pg.evaluate("() => document.getElementById('tempBanner').hidden")

        # Discard from the guard lands on the home screen with the
        # quick look gone for good.
        pg.locator("#btnHome").click()
        pg.wait_for_selector("#modal:not([hidden])")
        pg.locator("#modal button", has_text="Discard").click()
        pg.wait_for_selector(".confirm-overlay")
        pg.locator(".confirm-card .btn", has_text="Discard").click()
        pg.wait_for_selector("#home:not([hidden])", timeout=15_000)
        cur = pg.evaluate("""() => fetch('/api/case/current',
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
        assert cur["open"] is False
