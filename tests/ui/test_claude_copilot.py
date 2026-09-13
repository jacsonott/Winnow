"""The SQL Copilot end to end in the browser, with the Anthropic call
stubbed at the route: the panel mounts beside the pane, a question gets
an answer whose SQL block carries Insert and Run, and those drive the
editor and the result pane through winnow.sqlPage.
"""
import json

import pytest

pytestmark = pytest.mark.ui

ANSWER = {
    "answer": "Here you go:\n```sql\nSELECT rid, EventId FROM src_1 ORDER BY rid LIMIT 3\n```\nThree rows, by rid.",
    "model": "claude-opus-5", "stop_reason": "end_turn",
    "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 100},
}


@pytest.fixture
def copilot(page, server_post):
    server_post("/api/plugins/toggle", {"fs_name": "claude_assistant", "scope": "on_all"})
    page.evaluate("() => __winnow.loadPlugins()")
    page.wait_for_function("() => (__winnow.S.pluginPagePanels || []).some((p) => p.id === 'claude-assistant.copilot')")
    yield
    page.evaluate("() => { __winnow.togglePluginPanel('claude-assistant.copilot', false); localStorage.removeItem('winnow.panels'); }")
    server_post("/api/plugins/toggle", {"fs_name": "claude_assistant", "scope": "off_all"})
    page.evaluate("() => __winnow.loadPlugins()")


def test_copilot_answers_insert_and_run(page, copilot):
    seen = []

    def fake_ask(route):
        seen.append(json.loads(route.request.post_data))
        route.fulfill(status=200, content_type="application/json", body=json.dumps(ANSWER))
    page.route("**/api/plugin/claude_assistant/ask", fake_ask)

    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    page.wait_for_function("() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled")
    page.locator("#sqlText").fill("SELECT 'before'")
    btn = page.locator("#sqlPluginButtons .plugin-panel-btn", has_text="Copilot")
    btn.click()
    panel = page.locator("#sqlPluginPanels:not([hidden]) .plugin-panel")
    panel.locator("textarea").wait_for(state="visible")
    # The intro rendered from the (empty) copilot history, no network involved
    assert "Ask for a query" in panel.inner_text()

    panel.locator("textarea").fill("first three rows")
    panel.locator("textarea").press("Enter")
    panel.locator("button", has_text="Insert").wait_for(state="visible")
    # The request carried the mode, the schema and the editor's text
    assert seen[0]["mode"] == "sql"
    assert seen[0]["current_sql"] == "SELECT 'before'"
    assert "src_1" in seen[0]["schema"]
    assert panel.locator("pre").inner_text() == "SELECT rid, EventId FROM src_1 ORDER BY rid LIMIT 3"

    panel.locator("button", has_text="Insert").click()
    page.wait_for_function("() => document.getElementById('sqlText').value === 'SELECT rid, EventId FROM src_1 ORDER BY rid LIMIT 3'")
    panel.locator("button", has_text="Run").click()
    page.wait_for_selector("#sqlResult table")
    assert page.locator("#sqlResult tbody tr, #sqlResult tr").count() >= 3
    panel.locator(".note-status", has_text="3 rows").wait_for(state="visible")
