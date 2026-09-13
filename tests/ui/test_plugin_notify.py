"""winnow.notify: a plugin's row in the jobs panel, driven by a handle.

Asserts the things a plugin author would notice: the row is there with a
bar, update moves the bar, done with a button waits and the button both
navigates and closes the row, a plain done row fades, an error row does
not, a closed handle is inert, and a case switch clears the lot.
"""
import pytest

pytestmark = pytest.mark.ui

ROWS = "#jobsPanel:not([hidden]) .job-row"


def _bar_width(page):
    return page.locator(f"{ROWS} .job-bar-fill").first.evaluate("(e) => e.style.width")


def test_row_appears_and_update_moves_the_bar(page, fake_plugin_mount):
    page.evaluate("() => { window.__n = __ctx.notify({ title: 'VT lookups', detail: '0 / 40', progress: 0.3 }); }")
    page.wait_for_selector(ROWS)
    assert page.locator(f"{ROWS} .job-name").inner_text() == "VT lookups"
    assert page.locator(f"{ROWS} .job-phase").inner_text().lower() == "running"
    assert page.locator(f"{ROWS} .job-detail").inner_text() == "0 / 40"
    assert _bar_width(page) == "30%"
    page.evaluate("() => __n.update({ detail: '24 / 40', progress: 0.6 })")
    assert _bar_width(page) == "60%"
    assert page.locator(f"{ROWS} .job-detail").inner_text() == "24 / 40"
    # A free-text phase changes the badge text, never its class.
    page.evaluate("() => __n.update({ phase: 'thinking' })")
    badge = page.locator(f"{ROWS} .job-phase")
    assert badge.inner_text().lower() == "thinking"
    assert "running" in badge.get_attribute("class")
    page.evaluate("() => __n.close()")
    assert page.locator(ROWS).count() == 0


def test_done_with_a_button_waits_and_the_button_navigates(page, fake_plugin_mount):
    page.evaluate("() => { window.__clicked = false; window.__n = __ctx.notify({ title: 'Claude' });"
                  " __n.done({ detail: 'answered', actions: [{ label: 'Open Claude', onClick: () => { window.__clicked = true; __ctx.showTab(); } }] }); }")
    page.wait_for_selector(f"{ROWS} .job-action")
    assert page.locator(f"{ROWS} .job-phase").inner_text().lower() == "done"
    assert page.locator(f"{ROWS} .job-bar").count() == 0
    page.locator(f"{ROWS} .job-action", has_text="Open Claude").click()
    page.wait_for_function("() => window.__clicked === true && __winnow.S.activeTab === 'plugin:fake.t'")
    assert page.locator(ROWS).count() == 0
    # The tab is on the strip (closed tabs stay in the DOM, hidden)
    assert page.locator("#pageTabs button.tab-plugin:not([hidden])", has_text="Fake").count() == 1


def test_show_tab_reopens_a_closed_page_tab(page, fake_plugin_mount):
    page.evaluate("() => __winnow.closePageTab('plugin:fake.t')")
    assert page.locator("#pageTabs button.tab-plugin:not([hidden])", has_text="Fake").count() == 0
    page.evaluate("() => { __ctx.showTab(); }")
    page.wait_for_function("() => __winnow.S.activeTab === 'plugin:fake.t'")
    assert page.locator("#pageTabs button.tab-plugin:not([hidden])", has_text="Fake").count() == 1


def test_plain_done_fades_and_error_stays(page, fake_plugin_mount):
    page.evaluate("() => { __ctx.notify({ title: 'quick' }).done({ detail: 'ok' });"
                  " __ctx.notify({ title: 'broken' }).fail({ detail: 'boom' }); }")
    page.wait_for_selector(ROWS)
    assert page.locator(ROWS).count() == 2
    page.wait_for_function(f"() => document.querySelectorAll('{ROWS}').length === 1", timeout=12_000)
    assert page.locator(f"{ROWS} .job-name").inner_text() == "broken"
    assert "error" in page.locator(f"{ROWS} .job-phase").get_attribute("class")
    page.locator(f"{ROWS} .job-x").click()
    assert page.locator(ROWS).count() == 0


def test_a_closed_handle_is_inert_and_a_case_switch_clears_rows(page, fake_plugin_mount):
    page.evaluate("() => { window.__n = __ctx.notify({ title: 'late' }); __n.close(); __n.update({ detail: 'zombie' }); __n.done(); }")
    assert page.locator(ROWS).count() == 0
    assert page.evaluate("() => __n.open") is False
    page.evaluate("() => { __ctx.notify({ title: 'a' }); __ctx.notify({ title: 'b' }).fail(); }")
    assert page.locator(ROWS).count() == 2
    page.evaluate("() => __winnow.resetJobState()")
    assert page.locator(ROWS).count() == 0
    assert page.evaluate("() => document.getElementById('jobsPanel').hidden") is True


def test_a_mount_teardown_closes_only_its_own_rows(page, fake_plugin_mount):
    page.evaluate("() => { __ctx.notify({ title: 'mine' }).fail();"
                  " __winnow.createNotice('panel:other.p', { title: 'theirs' }).fail(); }")
    assert page.locator(ROWS).count() == 2
    page.evaluate("() => __winnow.disposePluginMount('tab:fake.t')")
    assert page.locator(ROWS).count() == 1
    assert page.locator(f"{ROWS} .job-name").inner_text() == "theirs"
    page.evaluate("() => __winnow.closeNoticesOwnedBy('panel:other.p')")
    assert page.locator(ROWS).count() == 0
