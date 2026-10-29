"""The histogram strip charts only the datetime columns the grid shows: a
column hidden from the table menu's Columns list is left out of the strip's
column list, hiding the charted one moves the chart to another without
waiting for a filter change, hiding them all says so, and showing the
analyst's pick again brings the chart back to it. A reload with the column
still hidden never asks the server about it."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from spawn import running_server

pytestmark = pytest.mark.ui

SELECT = "#histogramPanel .histogram-head > select"


@pytest.fixture
def own_server(tmp_path):
    """Its own case: two datetime columns a day apart, so the chart of one
    is not the chart of the other, and a layout these tests hide columns
    in without touching the shared case's."""
    csv_path = tmp_path / "two.csv"
    csv_path.write_text(
        "Created,Modified,Host\n"
        + "".join(f"2020-01-01 08:{i:02d}:00,2020-01-02 09:{i:02d}:00,H{i % 3}\n" for i in range(30)),
        encoding="utf-8")
    with running_server(tmp_path, "--case", tmp_path / "case.db", "--open", csv_path, "--no-fts",
                        env={"WINNOW_IDLE_EXIT_S": "600", "WINNOW_NEVER_CONNECTED_EXIT_S": "600"},
                        cwd=tmp_path, ready="/api/sources") as srv:
        yield srv


def _asked(urls):
    return [parse_qs(urlparse(u).query)["column"][0] for u in urls if urlparse(u).path == "/api/histogram"]


def _options(pg):
    return pg.evaluate(f"() => [...document.querySelector('{SELECT}').options].map((o) => o.value)")


def _charted(pg, name):
    """Wait for the chart on screen to be the answer for `name`."""
    pg.wait_for_function(
        """(name) => { const d = __winnow.histogramData();
             return !!d && d.column === name && d.total === 30
               && !document.getElementById('histogramPanel').classList.contains('is-outdated'); }""",
        arg=name, timeout=15_000)


def _set_shown(pg, name, on):
    pg.evaluate("() => __winnow.openTableMenu(__winnow.S.sourceId)")
    box = pg.locator(".collist-row", has_text=name).first.locator("input[type=checkbox]")
    box.wait_for(state="visible", timeout=10_000)
    if box.is_checked() != on:
        box.click()
    pg.wait_for_function("([n, on]) => !(__winnow.S.layout[n] || {}).hidden === on", arg=[name, on])
    pg.keyboard.press("Escape")


def test_hidden_columns_are_not_charted(own_page, own_server):
    with own_page(base=own_server.base, viewport=(1400, 900)) as pg:
        urls: list[str] = []
        pg.on("request", lambda r: urls.append(r.url))
        pg.evaluate("() => __winnow.toggleHistogram(true)")
        _charted(pg, "Created")
        assert _options(pg) == ["Created", "Modified"]
        # The analyst's pick, kept per browser.
        pg.locator(SELECT).select_option("Modified")
        _charted(pg, "Modified")

        # Hiding the charted column moves the chart, with no filter change
        # to prompt it, and takes the column out of the list.
        del urls[:]
        _set_shown(pg, "Modified", False)
        _charted(pg, "Created")
        assert _options(pg) == ["Created"]
        assert pg.locator(SELECT).input_value() == "Created"
        assert _asked(urls) == ["Created"]

        # Every datetime column hidden: a sentence that says why, not the
        # "no datetime column" one, and no column list.
        del urls[:]
        _set_shown(pg, "Created", False)
        pg.wait_for_selector("#histogramPanel .th-empty", state="visible", timeout=10_000)
        assert pg.locator("#histogramPanel .th-empty").inner_text() == (
            "Every datetime column in this table is hidden — "
            "show one from the table menu's Columns list to chart it.")
        assert pg.locator(SELECT).is_hidden()
        assert pg.locator("#histogramPanel canvas.th-canvas").is_hidden()
        assert _asked(urls) == []

        # Showing the analyst's pick again charts it again: hiding and
        # showing a column is a round trip, not a new choice.
        _set_shown(pg, "Modified", True)
        _charted(pg, "Modified")
        assert _options(pg) == ["Modified"]
        _set_shown(pg, "Created", True)
        assert _options(pg) == ["Created", "Modified"]
        assert pg.locator(SELECT).input_value() == "Modified"
        _charted(pg, "Modified")

        # Hidden in the saved layout: the strip opens on the column that is
        # shown and never names the hidden one to the server.
        _set_shown(pg, "Modified", False)
        _charted(pg, "Created")
        pg.evaluate("() => __winnow.saveLayoutNow()")
        del urls[:]
        pg.reload(wait_until="networkidle")
        pg.wait_for_selector(".row", timeout=30_000)
        _charted(pg, "Created")
        assert "Modified" not in _asked(urls)
        assert _options(pg) == ["Created"]
