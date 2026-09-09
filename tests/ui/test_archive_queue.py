"""Frontend routing for evidence archives: recognized by every import
gate, queued as kind 'archive' with nothing to configure (the expand step
opens directory import — driven end-to-end by tests/test_archive_ingest.py
plus the directory-import suite)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_archives_route_as_their_own_kind(page):
    for name in ("esx-support.zip", "uac-host.tar.gz", "bundle.tgz", "auth.log.1.gz"):
        assert page.evaluate("(n) => __winnow.recognizedImportFile(n)", name), name
        assert page.evaluate("(n) => __winnow.importKindFor(n)", name) == "archive", name
    item = page.evaluate("() => __winnow.queueItem({ path: '/x/esx-support.zip' }, 'esx-support.zip')")
    assert item["kind"] == "archive"
    assert item["configured"] is True
    # …and no plugin format claims the extensions away from the built-in.
    assert page.evaluate("() => __winnow.pluginFormatFor('a.zip')") in (None, False)
