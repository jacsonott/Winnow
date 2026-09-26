"""Getting to the profiles manager.

It was reachable by one hotkey and nothing else — fine once you know it
exists, no help at all before then. Settings is where someone looks for
"what else is there", so it has a section, and the key moves M → p.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_settings_profiles(page):
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    page.evaluate("""() => {
      const heads = [...document.querySelectorAll('#modal .settings-section-head')];
      const h = heads.find((x) => x.querySelector('.settings-section-title').textContent.trim() === 'Profiles');
      if (h && h.getAttribute('aria-expanded') !== 'true') h.click();
    }""")
    page.wait_for_selector("#modalBody .settings-line-text", state="attached")


def _profiles_box(page):
    return page.evaluate("""() => {
      const box = [...document.querySelectorAll('#modalBody .settings-line')]
        .find((x) => /profile/i.test(x.textContent));
      return box ? { line: box.querySelector('.settings-line-text').textContent,
                     button: box.querySelector('button').textContent,
                     help: box.querySelector('.fb-help').textContent } : null;
    }""")


def _click_open_profiles(page):
    page.evaluate("""() => {
      const box = [...document.querySelectorAll('#modalBody .settings-line')]
        .find((x) => /profile/i.test(x.textContent));
      box.querySelector('button').click();
    }""")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Profiles'")
    # The body is built after listBundles() resolves, so the foot — and the
    # way back in it — arrives a beat after the title does.
    page.wait_for_selector("#modalBody .pm-foot", state="attached")


@pytest.fixture(autouse=True)
def _close(page):
    yield
    page.keyboard.press("Escape")
    page.evaluate("() => { document.getElementById('modal').hidden = true; }")


def test_p_opens_the_profiles_manager(page):
    assert page.evaluate("() => __winnow.S.keymap.openPluginBundles") == ["p"]
    page.locator("#body").focus()
    page.keyboard.press("p")
    page.wait_for_selector("#modal:not([hidden])", timeout=5_000)
    assert page.evaluate("() => document.getElementById('modalTitle').textContent") == "Profiles"


def test_settings_has_a_way_in_and_counts_what_is_there(page):
    _open_settings_profiles(page)
    box = _profiles_box(page)
    assert box, "no Profiles section in Settings"
    # There are always the shipped ones, so a count is a real assertion
    # rather than a tautology — and the placeholder must have been replaced.
    assert "profile" in box["line"] and "shipped" in box["line"], box["line"]
    assert box["button"].startswith("Open profiles")
    # The prose names the key by reading the binding, so a rebinding in the
    # panel above cannot leave it naming a key that no longer works.
    assert "Also on p." in box["help"], box["help"]


def test_the_prose_names_whatever_the_key_is_rebound_to(page):
    page.evaluate("() => { __winnow.S.keymap.openPluginBundles = ['Z']; }")
    try:
        _open_settings_profiles(page)
        assert "Also on Z." in _profiles_box(page)["help"]
    finally:
        page.evaluate("() => { __winnow.S.keymap.openPluginBundles = ['p']; }")


def test_opened_from_settings_it_goes_back_to_settings(page):
    """The modal is shared, so without a way back the × drops the analyst on
    the grid — one level up, not where they came from."""
    _open_settings_profiles(page)
    _click_open_profiles(page)
    assert page.evaluate("() => !!document.querySelector('.pm-foot .pm-mini')"), "no back button"

    page.keyboard.press("Escape")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Settings'",
                           timeout=5_000)
    assert page.evaluate("() => document.getElementById('modal').hidden") is False


def test_the_back_button_goes_back_too(page):
    _open_settings_profiles(page)
    _click_open_profiles(page)
    page.evaluate("() => document.querySelector('.pm-foot .pm-mini').click()")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Settings'",
                           timeout=5_000)


def test_opened_by_the_key_it_has_no_way_back(page):
    """The one-shot listener is armed per opening. A Profiles opened from
    the keyboard must not inherit a return armed by an earlier one."""
    _open_settings_profiles(page)
    _click_open_profiles(page)
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Settings'")
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")

    page.locator("#body").focus()
    page.keyboard.press("p")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Profiles'")
    assert page.evaluate("() => !!document.querySelector('.pm-foot .pm-mini')") is False
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached", timeout=5_000)


def test_keymap_v6_migration_moves_an_untouched_binding(browser, server, first_run_init):
    """loadKeymap persists the whole default map on a profile's first load,
    so without a migration the new key reaches nobody who has run Winnow
    before — they keep pressing M and Settings keeps saying p."""
    ctx = browser.new_context()
    ctx.add_init_script(
        first_run_init + ";"
        "localStorage.setItem('winnow.keymap', JSON.stringify({ openPluginBundles: ['M'] }));"
        "localStorage.setItem('winnow.keymap.v', '5');")
    pg = ctx.new_page()
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_selector(".row", timeout=30_000)
        assert pg.evaluate("() => __winnow.S.keymap.openPluginBundles") == ["p"]
        pg.locator("#body").focus()
        pg.keyboard.press("p")
        pg.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Profiles'",
                             timeout=5_000)
    finally:
        ctx.close()


def test_keymap_v6_leaves_a_rebound_profiles_key_alone(browser, server, first_run_init):
    ctx = browser.new_context()
    ctx.add_init_script(
        first_run_init + ";"
        "localStorage.setItem('winnow.keymap', JSON.stringify({ openPluginBundles: ['Z'] }));"
        "localStorage.setItem('winnow.keymap.v', '5');")
    pg = ctx.new_page()
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_selector(".row", timeout=30_000)
        assert pg.evaluate("() => __winnow.S.keymap.openPluginBundles") == ["Z"]
    finally:
        ctx.close()


def test_keymap_v6_refuses_to_take_a_p_somebody_else_holds(browser, server, first_run_init):
    """`p` was unbound by default, so Settings accepted it for any action.
    Taking it here would shadow that binding — and because a migration edits
    the STORED map which is then merged OVER the defaults, merely declining
    to move would let the new default `p` arrive beside theirs. The refusal
    has to be written down to hold."""
    ctx = browser.new_context()
    ctx.add_init_script(
        first_run_init + ";"
        "localStorage.setItem('winnow.keymap', JSON.stringify({ toggleDetail: ['p'] }));"
        "localStorage.setItem('winnow.keymap.v', '5');")
    pg = ctx.new_page()
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_selector(".row", timeout=30_000)
        assert pg.evaluate("() => __winnow.S.keymap.toggleDetail") == ["p"]
        assert pg.evaluate("() => __winnow.S.keymap.openPluginBundles") == ["M"], \
            "the new default took a key the analyst had already bound"
        # And `p` really does the thing they bound it to, not both things.
        # toggleDetailPane needs a cursor row to open onto.
        pg.evaluate("() => __winnow.moveCursor(3)")
        pg.locator("#body").focus()
        pg.keyboard.press("p")
        pg.wait_for_function("() => !document.getElementById('detail').hidden", timeout=5_000)
        assert pg.evaluate("() => document.getElementById('modal').hidden") is True
    finally:
        ctx.close()
