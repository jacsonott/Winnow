"""Settings as two panes: the sections listed on the left, one section's
contents on the right.

It was thirteen collapsible sections in one column — only one readable
at a time, the last ones a long scroll down, and Appearance holding
settings that were not about the look. What the layout itself has to get
right, and what these hold it to: the right section is the one on screen
whichever way Settings was opened (the last one shown, a deep link, a
dialog handing the analyst back), the list works from the keyboard, and
the settings that moved out of Appearance are where the docs now say.
The card holding still while sections change is in
test_settings_ergonomics.py and test_visual_pass.py.
"""

from __future__ import annotations

import json
import re
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _open(page, where=None):
    page.evaluate("(w) => __winnow.openSettings(w || undefined)", where)
    page.wait_for_selector("#modalBody .settings-nav-item")


def _shown(page):
    """The section on screen, and the one the list marks — read separately,
    since the bug this guards against is the two disagreeing."""
    return page.evaluate("""() => ({
      panes: [...document.querySelectorAll('#modalBody .settings-pane')]
        .filter((p) => !p.hidden).map((p) => p.dataset.section),
      selected: [...document.querySelectorAll('#modalBody .settings-nav-item')]
        .filter((i) => i.getAttribute('aria-selected') === 'true').map((i) => i.dataset.section),
    })""")


def _pick(page, title):
    page.locator("#modalBody .settings-nav-item", has_text=re.compile(f"^{re.escape(title)}$")).click()


def _close(page):
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_it_opens_where_it_was_left(page):
    _open(page)
    assert _shown(page) == {"panes": ["appearance"], "selected": ["appearance"]}
    _pick(page, "Timestamps")
    _close(page)
    # The ⚙ button, the way an analyst comes back — its click event is not
    # a section name, so it must not be read as one.
    page.click("#btnSettings")
    page.wait_for_selector("#modalBody .settings-nav-item")
    assert _shown(page) == {"panes": ["timestamps"], "selected": ["timestamps"]}
    _close(page)
    # ...and after a reload: it is remembered in this browser, not in memory.
    page.reload(wait_until="networkidle")
    page.wait_for_selector(".row")
    _open(page)
    assert _shown(page) == {"panes": ["timestamps"], "selected": ["timestamps"]}
    _close(page)


def test_a_remembered_section_that_no_longer_exists_opens_the_first(page):
    page.evaluate("() => localStorage.setItem(__winnow.SETTINGS_SECTION_KEY, 'gone-in-a-later-build')")
    _open(page)
    assert _shown(page) == {"panes": ["appearance"], "selected": ["appearance"]}
    _close(page)


def test_a_deep_link_opens_its_section(page):
    _open(page, "plugins")
    assert _shown(page) == {"panes": ["plugins"], "selected": ["plugins"]}
    _close(page)
    _open(page, "no-such-section")
    assert _shown(page)["panes"] == ["appearance"]
    _close(page)


@pytest.mark.parametrize("section, button, title", [
    ("plugins", "Manage plugins…", "Plugins"),
    ("profiles", "Open profiles…", "Profiles"),
    ("filters", "Open saved filters…", "Saved filters"),
])
def test_a_dialog_opened_from_a_section_hands_back_to_that_section(page, section, button, title):
    """The remembered section is shared by every window on this origin, so
    "back to Settings" cannot lean on it: another Winnow window may have
    shown a different section in the meantime. The way back names its own."""
    _open(page, section)
    page.locator(f"#modalBody .settings-pane[data-section='{section}'] .btn", has_text=button).click()
    page.wait_for_function("(t) => document.getElementById('modalTitle').textContent === t", arg=title)
    page.wait_for_function("() => document.getElementById('modalBody').querySelector('.pm-foot, .sf-back')")
    page.evaluate("() => localStorage.setItem(__winnow.SETTINGS_SECTION_KEY, 'keys')")   # the other window
    page.click("#modalClose")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Settings'")
    assert _shown(page) == {"panes": [section], "selected": [section]}
    _close(page)


def test_the_skin_picker_hands_back_to_appearance(page):
    _open(page, "appearance")
    page.locator("#modalBody .btn", has_text="Change skin…").click()
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Skins'")
    page.evaluate("() => localStorage.setItem(__winnow.SETTINGS_SECTION_KEY, 'keys')")
    page.locator("#modalBody .btn", has_text="Done").click()
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Settings'")
    assert _shown(page)["panes"] == ["appearance"]
    _close(page)


def test_the_list_works_from_the_keyboard(page):
    page.locator("#body").focus()
    cursor = page.evaluate("() => __winnow.S.cursor")
    page.keyboard.press("?")
    page.wait_for_selector("#modalBody .settings-nav-item")
    # The chosen section has the focus on arrival, and is the list's only
    # tab stop — a tablist is one stop, not fifteen.
    focused = page.evaluate("() => document.activeElement.dataset.section")
    assert focused == _shown(page)["selected"][0] == "appearance"
    stops = page.evaluate("""() => [...document.querySelectorAll('#modalBody .settings-nav-item')]
      .filter((i) => i.tabIndex === 0).map((i) => i.dataset.section)""")
    assert stops == ["appearance"], stops

    page.keyboard.press("ArrowDown")
    assert _shown(page) == {"panes": ["grid"], "selected": ["grid"]}
    assert page.evaluate("() => document.activeElement.dataset.section") == "grid"
    page.keyboard.press("End")
    assert _shown(page)["panes"] == ["shutdown"]
    page.keyboard.press("ArrowDown")   # wraps
    assert _shown(page)["panes"] == ["appearance"]
    page.keyboard.press("ArrowUp")     # and back
    assert _shown(page)["panes"] == ["shutdown"]
    page.keyboard.press("Home")
    assert _shown(page)["panes"] == ["appearance"]
    # The arrows drive the list only — the grid's cursor behind the dialog
    # stays where it was — and the dialog's own key still closes it.
    assert page.evaluate("() => __winnow.S.cursor") == cursor
    page.keyboard.press("?")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_appearance_keeps_the_look_and_the_rest_moved_out(page):
    """Each moved setting keeps its own label, and sits in the section the
    docs name for it."""
    _open(page)

    def labels(section):
        _pick(page, section)
        return page.evaluate("""() => [...document.querySelectorAll(
            '#modalBody .settings-pane:not([hidden]) label, #modalBody .settings-pane:not([hidden]) .settings-sub-label')]
          .map((n) => n.textContent.trim())""")

    look = labels("Appearance")
    grid = labels("Grid & layout")
    remote = labels("Remote session")
    for moved in ("Always-on filter row", "Pages as a dropdown", "Remote session mode", "No limit"):
        assert not any(moved in t for t in look), (moved, look)
    assert "Always-on filter row" in grid and "Pages as a dropdown" in grid, grid
    assert any(t.lower() == "autofit column width limit" for t in grid), grid
    assert remote == ["Remote session mode"], remote
    for stays in ("Striped rows", "Launch animation"):
        assert stays in look, (stays, look)
    _close(page)


@pytest.fixture
def _remote_off_after(server):
    yield
    req = urllib.request.Request(
        server.rstrip("/") + "/api/settings/app",
        data=json.dumps({"remote_session": False}).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    urllib.request.urlopen(req, timeout=5).read()


def test_the_first_run_offer_names_the_section_that_holds_the_setting(own_page, _remote_off_after):
    """The offer says where to change the answer later. It said Appearance
    after the setting had left it, which sends someone hunting."""
    with own_page(seed="", ready=".confirm-overlay") as pg:   # a browser that has never seen Winnow
        said = pg.locator(".confirm-card").inner_text()
        m = re.search(r"Settings → ([^.)]+)", said)
        assert m, said
        pg.locator(".confirm-card .btn", has_text="No, local session").click()
        pg.wait_for_selector(".confirm-overlay", state="detached")
        pg.evaluate("() => __winnow.openSettings()")
        pg.wait_for_selector("#modalBody .settings-nav-item")
        pg.locator("#modalBody .settings-nav-item", has_text=re.compile(f"^{re.escape(m.group(1))}$")).click()
        assert pg.locator("#modalBody .settings-pane:not([hidden]) label",
                          has_text="Remote session mode").is_visible()
