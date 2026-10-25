"""Telling the analyst that Windows will refuse files dragged into a Winnow
started as administrator.

The probes themselves are Win32 calls and can only run on Windows, so
drops_blocked takes them as arguments and these drive the decision through
every combination with nothing real behind it — the same approach
test_browser_launch.py takes to browser discovery."""

from __future__ import annotations

import server
from winnow import elevation


def _blocked(own, shell):
    return elevation.drops_blocked(system="Windows",
                                   self_elevated=lambda: own, shell_elevated=lambda: shell)


def test_an_elevated_winnow_under_a_normal_explorer_is_blocked():
    """The reported case: started from an administrator PowerShell, every
    drag from Explorer showed a no-entry cursor."""
    assert _blocked(True, False) is True


def test_a_normal_winnow_is_not_blocked():
    assert _blocked(False, False) is False


def test_both_elevated_is_not_blocked():
    """UAC off, or the built-in Administrator: Explorer is elevated too, and
    drops between two elevated programs work, so saying otherwise is noise."""
    assert _blocked(True, True) is False


def test_an_unreadable_shell_falls_back_to_winnows_own_elevation():
    assert _blocked(True, None) is True
    assert _blocked(None, None) is False


def test_other_platforms_never_probe():
    def boom():
        raise AssertionError("probed off Windows")
    for system in ("Linux", "Darwin"):
        assert elevation.drops_blocked(system=system, self_elevated=boom, shell_elevated=boom) is False


def test_the_route_reports_it_to_a_window_on_this_machine(client, monkeypatch):
    monkeypatch.setattr(elevation, "this_process", lambda: True)
    assert client.get("/api/elevation").json() == {"drops_blocked": True}


def test_the_route_is_quiet_when_winnow_is_not_elevated(client, monkeypatch):
    monkeypatch.setattr(elevation, "this_process", lambda: False)
    assert client.get("/api/elevation").json() == {"drops_blocked": False}


def test_a_browser_on_another_machine_is_not_told(client, monkeypatch):
    """Its drops go through its own browser, with its own rights."""
    monkeypatch.setattr(elevation, "this_process", lambda: True)
    monkeypatch.setattr(server, "_is_loopback", lambda request: False)
    assert client.get("/api/elevation").json() == {"drops_blocked": False}
