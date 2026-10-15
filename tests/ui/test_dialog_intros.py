"""Sessions, Case settings, Import and the Timeframe filter opened on a
paragraph of three or four lines before the first thing you could use.
Each intro is one line now; the longer account is on the wiki page for
that dialog. Measured against a one-line probe of the same paragraph, in
Phosphor too, whose monospace chrome is the widest face the skins use."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

DIALOGS = [
    ("Sessions", "() => __winnow.openSessionManager()"),
    ("Case settings", "() => __winnow.openCaseSettings()"),
    ("Import", "() => __winnow.openImportModal()"),
    ("Timeframe filter", "() => __winnow.openTimeRangeModal()"),
]

LINES = """() => {
  const p = document.querySelector('#modalBody > p');
  const probe = p.cloneNode(false);
  probe.textContent = 'x';
  p.after(probe);
  const lines = p.getBoundingClientRect().height / probe.getBoundingClientRect().height;
  probe.remove();
  return { text: p.textContent, lines };
}"""


@pytest.fixture(autouse=True)
def _restore_skin(page):
    yield
    page.evaluate("() => { __winnow.applyStyle('harvest'); __winnow.applyThemeMode('dark'); }")


@pytest.mark.parametrize("skin", ["harvest", "phosphor"])
@pytest.mark.parametrize("title,opener", DIALOGS, ids=[d[0] for d in DIALOGS])
def test_the_intro_is_one_line(page, skin, title, opener):
    page.evaluate("(s) => __winnow.applyStyle(s)", skin)
    page.evaluate(opener)
    page.wait_for_function("(t) => document.getElementById('modalTitle').textContent === t", arg=title)
    got = page.evaluate(LINES)
    page.keyboard.press("Escape")
    assert got["lines"] < 1.5, got
