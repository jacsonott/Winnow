"""Installing a plugin from disk: Settings → Plugins → Manage plugins… →
Install a plugin….

The install route is tested in Python. What runs in the page had never run
in a browser: the dialog's own rules — the file button takes a .py, the
folder button a folder with __init__.py at its top level, both checked
before any bytes move — and installFiles, which builds the upload, asks
before replacing a plugin whose name is taken, and reopens the manager on
the plugin that just arrived.

The route is answered here, not by the server: a real install writes a .py
into the shared server's plugins folder and imports it, and nothing takes a
plugin back out. The upload the page sent is what these assert on, and the
answer is the server's own listing, so the manager repaints from the real
thing.
"""

from __future__ import annotations

import json
from email.parser import BytesParser
from email.policy import default as default_policy

import pytest

pytestmark = pytest.mark.ui

INSTALL = "**/api/plugins/install"


def _form(request):
    """A multipart/form-data body as {field: [(filename, bytes), ...]}."""
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    msg = BytesParser(policy=default_policy).parsebytes(head + request.post_data_buffer)
    form: dict[str, list[tuple[str | None, bytes]]] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        form.setdefault(name, []).append((part.get_filename(), part.get_payload(decode=True)))
    return form


class Install:
    """Answers the install route from a queue, and keeps every upload."""

    def __init__(self, page):
        self.page, self.sent, self.answers = page, [], []
        self.listing = page.evaluate("() => fetch('/api/plugins').then((r) => r.json())")
        # Not the first plugin listed: the manager opens on the first by
        # default, so reopening there would prove nothing.
        names = [p["fs_name"] for p in self.listing["plugins"]]
        assert len(names) > 1, "the bundled examples should be listed"
        self.arrived = names[-1]

    def installed(self, error=None):
        body = {**self.listing, "installed": self.arrived, **({"error": error} if error else {})}
        self.answers.append((200, body))

    def taken(self, message):
        self.answers.append((409, {"detail": message}))

    def __call__(self, route):
        self.sent.append(_form(route.request))
        status, body = self.answers.pop(0)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def install(page):
    handler = Install(page)
    page.route(INSTALL, handler)
    try:
        yield handler
    finally:
        page.unroute(INSTALL)
        page.evaluate("() => { if (!document.getElementById('modal').hidden) __winnow.closeModal(); }")


def _open_dialog(page):
    page.evaluate("() => __winnow.openPluginManager()")
    page.locator("#modalBody .btn", has_text="Install a plugin…").click()
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Install a plugin'")


FILE_BUTTON = "Pick a .py file…"
FOLDER_BUTTON = "Pick a plugin folder…"


def _pick(page, button, path):
    """Click one of the dialog's two buttons and answer the picker it
    opens — so a button that opens no picker fails here, as it would for
    the analyst."""
    with page.expect_file_chooser() as chooser:
        page.locator("#modalBody label.btn", has_text=button).click()
    chooser.value.set_files(str(path))


def _title(page):
    """The modal's title as written; CSS shows it in capitals."""
    return page.evaluate("() => document.getElementById('modalTitle').textContent")


def _toast(page, text):
    page.wait_for_function("(t) => (document.getElementById('toast').textContent || '').includes(t)", arg=text)


def _manager_back_on(page, fs_name):
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Plugins'")
    label = next(p.get("name") or p["fs_name"] for p in page.evaluate("() => __winnow.S.plugins")
                 if p["fs_name"] == fs_name)
    current = page.locator("#modalBody .pm-item[aria-current='true'] .pm-item-label")
    current.wait_for()
    assert current.inner_text() == label


def test_a_py_file_is_uploaded_and_the_manager_reopens_on_it(page, install, tmp_path):
    plugin = tmp_path / "probe_plugin.py"
    plugin.write_text("PLUGIN = {'name': 'Probe'}\n\ndef register(api):\n    pass\n", encoding="utf-8")
    install.installed()
    _open_dialog(page)
    _pick(page, FILE_BUTTON, plugin)

    _manager_back_on(page, install.arrived)
    _toast(page, f"Installed {install.arrived}")
    [form] = install.sent
    # By name: a routed request's body carries the file parts' headers but
    # not their bytes, which Chromium streams from the file itself.
    assert [name for name, _ in form["files"]] == ["probe_plugin.py"]
    assert json.loads(form["paths"][0][1]) == ["probe_plugin.py"]
    assert "overwrite" not in form


def test_the_file_button_refuses_anything_but_a_py(page, install, tmp_path):
    notes = tmp_path / "README.txt"
    notes.write_text("not a plugin\n", encoding="utf-8")
    _open_dialog(page)
    _pick(page, FILE_BUTTON, notes)
    _toast(page, "isn't a .py file")
    # Still on the dialog, and nothing was sent.
    assert _title(page) == "Install a plugin"
    assert install.sent == []


def test_the_folder_button_refuses_a_folder_without_init_at_its_top(page, install, tmp_path):
    """The parent of the plugin folder is the usual mistake: its
    __init__.py is a level down."""
    (tmp_path / "parent" / "probe_pkg").mkdir(parents=True)
    (tmp_path / "parent" / "probe_pkg" / "__init__.py").write_text("PLUGIN = {}\n", encoding="utf-8")
    _open_dialog(page)
    _pick(page, FOLDER_BUTTON, tmp_path / "parent")
    _toast(page, "no __init__.py at its top level")
    assert _title(page) == "Install a plugin"
    assert install.sent == []


def test_a_folder_is_uploaded_with_each_file_s_path_inside_it(page, install, tmp_path):
    """The folder button did nothing for a month: moving the dialog into
    the manager dropped the input's type="file", leaving a hidden text
    box with webkitdirectory on it, which no click opens a picker for."""
    pkg = tmp_path / "probe_pkg"
    (pkg / "ui").mkdir(parents=True)
    (pkg / "__init__.py").write_text("PLUGIN = {'name': 'Probe pkg'}\n", encoding="utf-8")
    (pkg / "ui" / "main.js").write_text("export function mount() {}\n", encoding="utf-8")
    install.installed()
    _open_dialog(page)
    _pick(page, FOLDER_BUTTON, pkg)

    _manager_back_on(page, install.arrived)
    [form] = install.sent
    paths = json.loads(form["paths"][0][1])
    assert sorted(paths) == ["probe_pkg/__init__.py", "probe_pkg/ui/main.js"]
    # Each path is the one for the file sent in the same place: the
    # route pairs them by position.
    assert [name for name, _ in form["files"]] == paths


def test_a_name_already_taken_is_replaced_only_when_asked(page, install, tmp_path):
    plugin = tmp_path / "probe_plugin.py"
    plugin.write_text("PLUGIN = {}\n", encoding="utf-8")
    taken = "A plugin named probe_plugin is already installed"

    # Declined: one upload, and nothing replaced.
    install.taken(taken)
    _open_dialog(page)
    _pick(page, FILE_BUTTON, plugin)
    card = page.locator(".confirm-card")
    card.wait_for()
    assert f"{taken}. Replace it?" in card.inner_text()
    card.locator(".btn", has_text="Cancel").click()
    page.wait_for_selector(".confirm-overlay", state="detached")
    assert len(install.sent) == 1

    # Accepted: the same upload again, now saying overwrite.
    install.taken(taken)
    install.installed()
    _open_dialog(page)
    _pick(page, FILE_BUTTON, plugin)
    page.locator(".confirm-card .btn", has_text="Replace").click()
    _manager_back_on(page, install.arrived)
    first, second = install.sent[1:]
    assert "overwrite" not in first
    assert second["overwrite"] == [(None, b"true")]
    assert second["files"] == first["files"]


def test_a_plugin_that_installs_but_will_not_load_says_so(page, install, tmp_path):
    plugin = tmp_path / "probe_plugin.py"
    plugin.write_text("def register(api:\n", encoding="utf-8")
    install.installed(error="SyntaxError: '(' was never closed")
    _open_dialog(page)
    _pick(page, FILE_BUTTON, plugin)
    _toast(page, f"Installed {install.arrived}, but it failed to load: SyntaxError")
