"""With the esxi_logs plugin on, the folder-import modal offers a .log
chip (built from the plugin's extensions), on by default — the way an
analyst gets a whole support bundle past the scan's extension gate.
"""
import pytest

pytestmark = pytest.mark.ui


@pytest.fixture
def esxi_on(page, server_post):
    server_post("/api/plugins/toggle", {"fs_name": "esxi_logs", "scope": "on_all"})
    page.evaluate("() => __winnow.loadPlugins()")
    page.wait_for_function("() => (__winnow.S.pluginFormats || []).some((f) => f.id === 'esxi-logs.esxi_log')")
    yield
    page.evaluate("() => __winnow.closeModal()")
    server_post("/api/plugins/toggle", {"fs_name": "esxi_logs", "scope": "off_all"})
    page.evaluate("() => __winnow.loadPlugins()")


def test_log_chip_is_offered_and_on(page, esxi_on):
    page.evaluate("() => { __winnow.openDirectoryImportModal(); }")
    page.wait_for_selector("#modal:not([hidden])")
    chip = page.locator("#modalBody button", has_text=".log")
    chip.wait_for(state="visible")
    assert chip.get_attribute("aria-pressed") == "true"
    assert page.locator("#modalBody button", has_text=".csv").get_attribute("aria-pressed") == "true"
