"""Whether Windows will refuse a file dragged into Winnow's window.

Started from an administrator PowerShell (or with Run as administrator),
Winnow runs elevated, and so does the app window it opens: the browser
inherits the token. Windows does not let a program running normally, such
as File Explorer, drag and drop into one running as administrator (User
Interface Privilege Isolation), so every drag from Explorer shows a
no-entry cursor and the page never receives a single drag event. Nothing
in the page can notice that, so the server says so up front instead: a
warning at startup and a notice in the window (GET /api/elevation).

The test is "Winnow is elevated and the desktop shell is not", not just
"Winnow is elevated". With UAC switched off, or signed in as the built-in
Administrator, Explorer runs elevated too, and drops between two elevated
programs work, so a warning there would be noise. When the shell can't be
read at all, Winnow's own elevation decides.

It can still be wrong one way: when the browser was already open before
Winnow started, the window joins that browser and runs with its rights,
not Winnow's. So what the analyst is told is that dragging *may* not work,
and why, rather than that it won't.
"""

from __future__ import annotations

import functools
import platform


def drops_blocked(*, system=None, self_elevated=None, shell_elevated=None) -> bool:
    """True when Windows will likely refuse drags from Explorer into the
    windows this process opens. The keyword arguments exist so the tests
    can ask "what would this do on Windows" without being on Windows."""
    system = system or platform.system()
    if system != "Windows":
        return False
    if not (self_elevated or _self_elevated)():
        return False
    return (shell_elevated or _shell_elevated)() is not True


@functools.lru_cache(maxsize=1)
def this_process() -> bool:
    """drops_blocked() for the running server, worked out once: neither
    side's elevation changes while Winnow runs."""
    return drops_blocked()


# ----------------------------------------------------------------- windows
#
# Each probe answers True, False or None (couldn't tell). A probe that
# fails must never stop the server starting, so every one swallows its own
# errors. Function prototypes are set on private WinDLL instances, never on
# ctypes.windll's shared ones, which other code in the process also calls.

_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION = 20                    # TOKEN_INFORMATION_CLASS.TokenElevation
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _token_elevated(process) -> bool | None:  # pragma: no cover - windows only
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        elevated = wintypes.DWORD()      # TOKEN_ELEVATION is a single DWORD
        size = wintypes.DWORD()
        if not advapi32.GetTokenInformation(token, _TOKEN_ELEVATION, ctypes.byref(elevated),
                                            ctypes.sizeof(elevated), ctypes.byref(size)):
            return None
        return bool(elevated.value)
    finally:
        kernel32.CloseHandle(token)


def _self_elevated() -> bool | None:  # pragma: no cover - windows only
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        # A pseudo-handle: nothing to close.
        return _token_elevated(kernel32.GetCurrentProcess())
    except Exception:  # noqa: BLE001 — see the section comment
        return None


def _shell_elevated() -> bool | None:  # pragma: no cover - windows only
    """Whether the desktop shell (File Explorer, the usual drag source)
    runs elevated. GetShellWindow is the desktop window, which the shell
    process owns."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetShellWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        hwnd = user32.GetShellWindow()
        if not hwnd:
            return None                  # no shell running
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        process = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not process:
            return None
        try:
            return _token_elevated(process)
        finally:
            kernel32.CloseHandle(process)
    except Exception:  # noqa: BLE001 — see the section comment
        return None
