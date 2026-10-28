"""The histogram strip marks the days, or the weeks, behind its bars.

Asked for as "include day/week (based on the scale) markers in the table
histograms like we have in the timeline view": the Timeline's overview
strip drew a dashed rule at each midnight, and the histogram over the
grid drew nothing between its start, middle and end labels, so there was
no telling which bar was which night. Both strips now draw the same rules
(charts.js calendarRules): midnights while the bars are finer than a day,
the start of each week (Monday, UTC) once they are a day or more, or the
days are packed too tight to tell apart.

The rules' times are read from histogramRules(), what the strip last
painted; the day test also reads the canvas, in a night with no rows, so
a rule that is computed but never drawn fails here too. Each rule's date
is written under the axis where it fits, ISO like the start and end
labels beside it.
"""

from __future__ import annotations

import contextlib
import datetime
import time

import pytest

pytestmark = pytest.mark.ui

UTC = datetime.timezone.utc
DAY = 86400


def _epoch(d: datetime.datetime) -> int:
    return int(d.replace(tzinfo=UTC).timestamp())


@contextlib.contextmanager
def _charted(page, server_post, tmp_path, name, stamps):
    """A table of one When column holding `stamps`, open, with the
    histogram strip charting it. The table is deleted and the strip closed
    on the way out, whether or not the setup got that far."""
    f = tmp_path / name
    f.write_text("When,N\n" + "".join(f"{s:%Y-%m-%d %H:%M:%S},{i}\n" for i, s in enumerate(stamps)))
    sid = None
    try:
        server_post("/api/ingest/jobs/path", {"path": str(f), "name": name, "kind": "csv"})
        # Polled from Python: wait_for_function does not await a promise
        # predicate (tests/test_ui_test_hygiene.py).
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            names = page.evaluate(
                "() => __winnow.loadSources().then(() => __winnow.S.sources.map((s) => s.name))")
            if name in names:
                break
            time.sleep(0.25)
        else:
            raise AssertionError(f"{name} never appeared in S.sources")
        sid = page.evaluate("(n) => __winnow.S.sources.find((s) => s.name === n).id", name)
        page.evaluate("(id) => __winnow.openSource(id)", sid)
        page.wait_for_function(
            "([id, n]) => __winnow.S.sourceId === id && __winnow.S.view"
            " && __winnow.S.view.row_count === n && __winnow.busyCount === 0", arg=[sid, len(stamps)])
        btn = page.locator("#btnHistogram")
        btn.wait_for(state="visible", timeout=30_000)
        if btn.get_attribute("aria-pressed") == "false":
            btn.click()
        page.wait_for_selector("#pluginPanels:not([hidden]) #histogramPanel canvas.th-canvas", timeout=30_000)
        page.wait_for_function("(n) => document.getElementById('histogramPanel').textContent"
                               ".includes(`${n.toLocaleString()} rows`)", arg=len(stamps), timeout=30_000)
        page.wait_for_function("() => !!__winnow.histogramRules()")
        yield sid
    finally:
        page.evaluate("""async (id) => {
          const h = { 'X-Timeline-Lite-Client': '1' };
          if (id != null) {
            await fetch('/api/source/' + id, { method: 'DELETE', headers: h });
            __winnow.S.viewCache.delete(id);
          }
          await __winnow.loadSources();
          const first = __winnow.S.sources.find((s) => !s.is_merge);
          if (first) __winnow.openSource(first.id);
        }""", sid)
        page.evaluate("() => { __winnow.toggleHistogram(false); localStorage.removeItem('winnow.histogram'); }")
        page.wait_for_selector(".row")


def _painted_in_column(page, epoch):
    """How many pixels of the plot are painted in the canvas column where
    `epoch` lands, by the strip's own axis."""
    return page.evaluate("""(t) => {
      const d = __winnow.histogramData();
      const t0 = d.buckets[0][0];
      const t1 = Math.max(d.buckets[d.buckets.length - 1][0] + d.bucket_seconds, t0 + d.bucket_seconds);
      const c = document.querySelector('#histogramPanel canvas.th-canvas');
      const dpr = window.devicePixelRatio || 1;
      const x = Math.round((t - t0) / (t1 - t0) * c.clientWidth);
      // The plot only: below the 6px lane the position marks use, above
      // the baseline and its labels.
      const col = c.getContext('2d').getImageData(Math.round(x * dpr) + Math.floor(dpr / 2),
        Math.round(10 * dpr), 1, Math.round((96 - 16 - 12) * dpr)).data;
      let n = 0;
      for (let i = 3; i < col.length; i += 4) if (col[i] > 50) n++;
      return n;
    }""", epoch)


def test_midnights_are_marked_while_the_bars_are_finer_than_a_day(page, server_post, tmp_path):
    """Three working days, 08:00 to 18:00, so each midnight falls in a
    night with no rows: the rule is the only thing painted there."""
    days = [datetime.datetime(2026, 2, 10) + datetime.timedelta(days=d) for d in range(3)]
    stamps = [day + datetime.timedelta(hours=8, minutes=5 * i) for day in days for i in range(121)]
    with _charted(page, server_post, tmp_path, "rules_days.csv", stamps):
        assert page.evaluate("() => __winnow.histogramData().bucket_seconds") < DAY
        rules = page.evaluate("() => __winnow.histogramRules()")
        assert rules["unit"] == "day", rules
        assert rules["times"] == [_epoch(days[1]), _epoch(days[2])], rules
        # Three days across the strip leave room to date both under the
        # axis, in the ISO form the start and end labels are in.
        assert rules["labels"] == [{"t": _epoch(days[1]), "text": "2026-02-11"},
                                   {"t": _epoch(days[2]), "text": "2026-02-12"}], rules
        # Drawn, and only there: a dashed line down the plot at midnight,
        # nothing an hour and a half later in the same quiet night.
        assert _painted_in_column(page, _epoch(days[1])) > 10
        assert _painted_in_column(page, _epoch(days[1]) + 5400) == 0


def test_weeks_are_marked_once_the_bars_are_a_day(page, server_post, tmp_path):
    """Sixteen weeks from a Wednesday, a row every four hours: the bars
    are a day each, so a rule per midnight would be a rule per bar.
    Mondays instead, every one inside the span and no other day."""
    start = datetime.datetime(2026, 1, 7)
    stamps = [start + datetime.timedelta(hours=4 * i) for i in range(16 * 7 * 6)]
    with _charted(page, server_post, tmp_path, "rules_weeks.csv", stamps):
        assert page.evaluate("() => __winnow.histogramData().bucket_seconds") == DAY
        rules = page.evaluate("() => __winnow.histogramRules()")
        assert rules["unit"] == "week", rules
        when = [datetime.datetime.fromtimestamp(t, UTC) for t in rules["times"]]
        assert all(d.weekday() == 0 and d.time() == datetime.time(0) for d in when), when
        assert when[0].date() == datetime.date(2026, 1, 12), when
        assert all(b - a == datetime.timedelta(days=7) for a, b in zip(when, when[1:])), when
        assert stamps[-1] - when[-1].replace(tzinfo=None) < datetime.timedelta(days=7), when
        # Dated under the axis where they fit: some, only rules, each in ISO.
        assert rules["labels"], rules
        for label in rules["labels"]:
            assert label["t"] in rules["times"], rules
            assert label["text"] == f"{datetime.datetime.fromtimestamp(label['t'], UTC):%Y-%m-%d}", rules


def test_the_scale_decides_day_week_or_nothing(page):
    """calendarRules on its own, for the edges a table would take a lot of
    rows to reach: days packed too tight give way to weeks, weeks too
    tight to nothing, bars a week wide get nothing, and a span from before
    1970 still lands on midnights and Mondays."""
    def rules(t0, t1, bucket, px_per_day):
        return page.evaluate("([a, b, w, p]) => __winnow.calendarRules(a, b, w, p / 86400)",
                             [t0, t1, bucket, px_per_day])

    mon = _epoch(datetime.datetime(2026, 3, 9))
    # A span that starts on a midnight does not mark its own left edge.
    assert rules(mon, mon + 3 * DAY, 1800, 400) == {"unit": "day", "times": [mon + DAY, mon + 2 * DAY]}
    # Hour bars, but only 6px a day: weeks.
    wk = rules(mon - DAY, mon + 60 * DAY, 3600, 6)
    assert wk["unit"] == "week" and wk["times"][0] == mon and len(wk["times"]) == 9, wk
    # Day bars with weeks 5px apart, and bars a week wide: nothing.
    assert rules(mon, mon + 3 * 365 * DAY, DAY, 5 / 7) == {"unit": None, "times": []}
    assert rules(mon, mon + 3 * 365 * DAY, 7 * DAY, 50) == {"unit": None, "times": []}
    # Either side of the epoch.
    t0 = _epoch(datetime.datetime(1969, 12, 20, 6))
    t1 = _epoch(datetime.datetime(1970, 1, 20))
    days = rules(t0, t1, 3600, 40)
    assert days["unit"] == "day" and days["times"][0] == _epoch(datetime.datetime(1969, 12, 21)), days
    assert 0 in days["times"], days
    weeks = rules(t0, t1, DAY, 2)
    assert [datetime.datetime.fromtimestamp(t, UTC).date() for t in weeks["times"]] == [
        datetime.date(1969, 12, 22), datetime.date(1969, 12, 29), datetime.date(1970, 1, 5),
        datetime.date(1970, 1, 12), datetime.date(1970, 1, 19)], weeks
