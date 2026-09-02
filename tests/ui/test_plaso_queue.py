"""Frontend routing for .plaso files: recognized by the import gates and
queued as its own kind with nothing to configure."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_plaso_files_route_as_their_own_kind(page):
    assert page.evaluate("() => __winnow.recognizedImportFile('timeline.plaso')")
    assert page.evaluate("() => __winnow.importKindFor('timeline.plaso')") == "plaso"
    item = page.evaluate("() => __winnow.queueItem({ path: '/x/timeline.plaso' }, 'timeline.plaso')")
    assert item["kind"] == "plaso"
    assert item["configured"] is True
    # …and no plugin format claims the extension away from the built-in.
    assert page.evaluate("() => __winnow.pluginFormatFor && __winnow.pluginFormatFor('t.plaso')") in (None, False)
