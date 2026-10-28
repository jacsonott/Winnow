"""A double-click on a button that creates, imports, merges or copies does
it once. Each of these handlers awaits its POST(s) before closing its
dialog, and the button stayed live meanwhile, so the second click of a
double-click ran the whole handler again: every file of a folder import
queued twice, a second case created on the same path, two merges, a
second copy that found the target locked by the first.

Every POST the button makes is HELD (page.route) until the double-click
is over, so the second click cannot miss the first by being slower than
the server. The held requests are then answered with an error rather
than let through, so none of these tests leaves anything behind in the
session's shared case."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui


class _Gate:
    """Counts the POSTs to one route; holds them until release(), then
    refuses them (and any that come after) with a 400."""

    def __init__(self, page, glob):
        self.page, self.glob = page, glob
        self.held, self.count, self.open = [], 0, False
        page.route(glob, self._handle)

    def _refuse(self, route):
        route.fulfill(status=400, content_type="application/json",
                      body=json.dumps({"detail": "refused by the test"}))

    def _handle(self, route):
        if route.request.method != "POST":
            route.continue_()
            return
        self.count += 1
        if self.open:
            self._refuse(route)
        else:
            self.held.append(route)

    def wait_for_first(self):
        for _ in range(100):
            if self.count:
                return
            self.page.wait_for_timeout(50)
        raise AssertionError(f"nothing was POSTed to {self.glob}")

    def settle(self):
        """Give a second POST, if there is going to be one, a generous
        window to show up rather than looking once."""
        self.page.wait_for_timeout(500)

    def release(self):
        self.open = True
        for r in self.held:
            self._refuse(r)
        self.held = []

    def close(self):
        self.release()
        self.page.unroute(self.glob)


def test_import_checked_queues_each_file_once(page, tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    for name in ("a.csv", "b.csv"):
        (root / name).write_text("Host,User\nh1,alice\n", encoding="utf-8")
    page.evaluate("(root) => __winnow.openDirectoryImportModal({ root })", str(root))
    btn = page.locator("#modalBody button", has_text="Import checked (2)")
    btn.wait_for()
    gate = _Gate(page, "**/api/ingest/jobs/path")
    try:
        btn.dblclick()
        gate.wait_for_first()
        gate.settle()
        assert gate.count == 1, "the second click started a second import pass"
        gate.release()
        page.wait_for_selector("#modal[hidden]", state="attached")
        assert gate.count == 2, f"{gate.count} imports queued for 2 files"
    finally:
        gate.close()


def test_create_case_creates_once(page):
    page.evaluate("() => { __winnow.showHome(); return __winnow.refreshCases(); }")
    page.locator("#home .btn", has_text="New case").click()
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modalBody input").first.fill("Double clicked")
    create = page.locator("#modal .nc-go")
    page.wait_for_function("() => document.querySelector('#modal .nc-go').textContent.includes('Double clicked')")
    gate = _Gate(page, "**/api/cases")
    try:
        create.dblclick()
        gate.wait_for_first()
        gate.settle()
        assert gate.count == 1, f"{gate.count} cases created for one double-click"
    finally:
        gate.close()
    page.wait_for_selector("#toast:not([hidden])")
    page.keyboard.press("Escape")
    page.evaluate("() => __winnow.showApp()")


def test_create_merge_merges_once(page):
    # Two tables with one set of columns is all the builder needs to offer
    # a merge; the second is the first under another id, in client state
    # only — the POST it would make never reaches the server.
    page.evaluate("""() => {
      const s0 = __winnow.S.sources.find((s) => !s.is_merge && !s.error);
      const saved = __winnow.S.sources;
      __winnow.S.sources = [s0, { ...s0, id: 999001, name: 'twin.csv' }];
      try { __winnow.openMergeBuilder(); } finally { __winnow.S.sources = saved; }
    }""")
    page.locator("#modalBody button", has_text="Select all").first.click()
    create = page.locator("#modalBody button", has_text="Create merge")
    gate = _Gate(page, "**/api/merges")
    try:
        create.dblclick()
        gate.wait_for_first()
        gate.settle()
        assert gate.count == 1, f"{gate.count} merges for one double-click"
    finally:
        gate.close()
    page.wait_for_selector("#toast:not([hidden])")
    page.keyboard.press("Escape")


def test_copy_into_a_case_copies_once(page):
    """The quick-look copy's main button: the second click's copy found
    the target case locked by the first one's Store and said it was open
    in another Winnow — naming this one. A case is picked from the list
    first; the button names it."""
    page.evaluate("() => __winnow.tempCopyModal()")
    page.locator("#modalBody .copyto-row").first.click()
    go = page.locator("#modal .table-menu-foot .btn.primary")
    page.wait_for_function("() => /^Copy \\d+ tables? into /.test(document.querySelector('#modal .table-menu-foot .btn.primary').textContent)")
    gate = _Gate(page, "**/api/case/copy_sources")
    try:
        go.dblclick()
        gate.wait_for_first()
        gate.settle()
        assert gate.count == 1, f"{gate.count} copies for one double-click"
    finally:
        gate.close()
    page.wait_for_selector("#toast:not([hidden])")
    page.keyboard.press("Escape")
