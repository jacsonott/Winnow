"""Per-user environment variables Winnow is allowed to manage — the home
for tokens and other secrets a plugin needs, kept OUT of the case file
and out of Winnow's own settings.

The rule is a prefix: only `WINNOW_*` names can be set, removed or read
through this module, so the Settings panel and /api/env can never become
a general environment editor. On the read side the prefix is a
convention that keeps well-behaved plugins honest — a plugin is
arbitrary Python and can always call os.environ itself (see the guide's
security model); this module does not sandbox that. Names Winnow itself
reads for configuration (RESERVED) are never loaded from the store or
accepted for writes — they are command-line knobs, not secrets, and a
stored one would move the workspace out from under a running install.

Where a value is kept is the OS's own per-user environment, so it is
exactly as private as the account:

- Windows: `HKCU\\Environment` — the user's real environment variables,
  the ones the System Properties dialog edits. New processes see them;
  this one is updated in place.
- Everywhere else: a `NAME=value` file, `~/.config/winnow/env` (or
  `$XDG_CONFIG_HOME/winnow/env`), created 0600 in a 0700 directory.

Stored values are loaded into the process by main() for any `WINNOW_*`
name not already set outside Winnow — a foreign environment variable
always wins, and a name something else exported cannot be changed from
here (set_var refuses, so the panel can say so instead of pretending).

Either way they survive a restart: the next launch reads the store on
Unix, and on Windows the OS itself hands the variable to the new process
because HKCU\\Environment is where it lives.

Values are never returned by the API and never shown by the UI: the
panel lists names and where each came from, nothing more. A plugin gets
a value through `PluginRequest.env(name)`, server-side only; nothing
here is ever sent to the browser.

`WINNOW_ENV_FILE` relocates the file (tests point it at a tmp dir).
"""

from __future__ import annotations

import contextlib
import ntpath
import os
import re
import sys
import tempfile
from pathlib import Path

from . import assoc

PREFIX = "WINNOW_"
NAME_RE = re.compile(r"^WINNOW_[A-Z0-9_]{1,56}$")
# Winnow's own configuration knobs. Never loaded from the store, never
# editable from here. tests/test_userenv.py checks this against every
# WINNOW_* the code actually reads from os.environ.
RESERVED = frozenset({
    "WINNOW_WORKSPACE_DIR", "WINNOW_PLUGINS_DIR", "WINNOW_CASES_DIR", "WINNOW_ENV_FILE",
    "WINNOW_IDLE_EXIT_S", "WINNOW_IDLE_TICK_S", "WINNOW_NEVER_CONNECTED_EXIT_S",
})
MAX_VALUE = 8192

# Names THIS module put into os.environ (loaded at startup or saved this
# run). Anything else that is live came from outside, and that wins.
#
# On Windows the store IS the user environment (HKCU\Environment), so a
# launch AFTER a save inherits the variable from the OS and finds it in
# the store too. That is not a foreign export, it is our own value handed
# back — so ownership is decided by comparing values, not by mere
# presence, or the panel would call a saved token a shell export and
# refuse to let anyone change it.
_OWNED: set[str] = set()


def _foreign(name: str, stored: dict) -> bool:
    """Live in this process, not ours, and not equal to what we stored —
    i.e. genuinely set outside Winnow, which wins."""
    if name in _OWNED or name not in os.environ:
        return False
    return stored.get(name) != os.environ[name]


def check_name(name: str, *, for_write: bool = False) -> str:
    """The name, or ValueError saying what is wrong with it. Writes are
    case-normalised (the UI and curl agree on `WINNOW_TOKEN`)."""
    name = (name or "").strip()
    if for_write:
        name = name.upper()
    if not name.startswith(PREFIX):
        raise ValueError(f"Only {PREFIX}* variables can be managed here")
    if not NAME_RE.match(name):
        raise ValueError(f"A variable name is {PREFIX} followed by capitals, digits or _ (max 63)")
    if for_write and name in RESERVED:
        raise ValueError(f"{name} is one of Winnow's own settings — set it on the command line, not here")
    return name


def check_value(value: str) -> str:
    if value is None:
        raise ValueError("A value is required")
    value = str(value)
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("A value is a single line")
    if len(value) > MAX_VALUE:
        raise ValueError(f"Values are capped at {MAX_VALUE} characters")
    return value


# ----------------------------------------------------------------- stores

class FileEnvStore:
    """POSIX: one `NAME=value` per line, owner-only. Values are written
    verbatim (no quoting, no escapes), which is why check_value refuses
    newlines. Rewritten whole through a temp file so a crash mid-write
    can't leave half a token behind."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def location(self) -> str:
        return str(self.path)

    def load(self) -> dict[str, str]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        out = {}
        for line in text.splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if NAME_RE.match(k):      # a hand-typed name the API could never remove is left alone
                out[k] = v
        return out

    def _write(self, data: dict[str, str]) -> None:
        d = self.path.parent
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(d, 0o700)
        body = "".join(f"{k}={v}\n" for k, v in sorted(data.items()))
        fd, tmp = tempfile.mkstemp(prefix=".env-", dir=str(d))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def set(self, name: str, value: str) -> None:
        data = self.load()
        data[name] = value
        self._write(data)

    def delete(self, name: str) -> None:
        data = self.load()
        if name in data:
            del data[name]
            self._write(data)


class RegistryEnvStore:
    """Windows: values under HKCU\\Environment, which is what the OS reads
    to build a new process's environment for this user. Takes a `reg`
    object (the real `winreg` in production, a fake in tests) so the
    layout is testable off-Windows, the way assoc.WindowsAssoc does."""

    KEY = "Environment"

    def __init__(self, reg=None, notify=None):
        if reg is None:
            import winreg as reg  # pragma: no cover - windows only
        self.reg = reg
        self._notify = _broadcast_env_change if notify is None else notify

    def location(self) -> str:
        return "HKEY_CURRENT_USER\\Environment"

    def load(self) -> dict[str, str]:
        out = {}
        try:
            key = self.reg.OpenKey(self.reg.HKEY_CURRENT_USER, self.KEY, 0, self.reg.KEY_READ)
        except OSError:
            return out
        expand_kind = getattr(self.reg, "REG_EXPAND_SZ", None)
        try:
            i = 0
            while True:
                try:
                    name, value, kind = self.reg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if not isinstance(value, str) or not NAME_RE.match(str(name)):
                    continue   # binary/DWORD values are not ours; malformed names can't be removed
                out[str(name)] = ntpath.expandvars(value) if kind == expand_kind else value
        finally:
            self.reg.CloseKey(key)
        return out

    def set(self, name: str, value: str) -> None:
        assoc.hkcu_set(self.reg, self.KEY, name, value)
        self._notify()

    def delete(self, name: str) -> None:
        if assoc.hkcu_delete_value(self.reg, self.KEY, name):
            self._notify()


def _broadcast_env_change() -> None:  # pragma: no cover - windows only
    """Tell Explorer the user environment changed, so a shell opened from
    the Start menu after this sees the new value without a log-off."""
    try:
        import ctypes
        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", SMTO_ABORTIFHUNG, 2000, None)
    except Exception:  # noqa: BLE001 — cosmetic; the value is already saved
        pass


def env_file_path() -> Path:
    override = os.environ.get("WINNOW_ENV_FILE")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "winnow" / "env"


def store():
    """The store for this platform. Resolved per call so a test's env
    override and a Windows fake both take effect without global state."""
    if sys.platform == "win32" and not os.environ.get("WINNOW_ENV_FILE"):  # pragma: no cover
        return RegistryEnvStore()
    return FileEnvStore(env_file_path())


# ------------------------------------------------------------------- API

def load_into_environ(st=None) -> list[str]:
    """Startup (main()): put every stored WINNOW_* value into this
    process's environment unless the shell already exported that name —
    the real environment wins. RESERVED knobs are skipped: the store is
    for secrets, not for relocating Winnow's directories. Returns the
    names loaded."""
    loaded = []
    try:
        st = st or store()
        data = st.load()
    except Exception as e:  # noqa: BLE001 — a bad file must never block startup
        where = st.location() if st is not None else "the WINNOW_* environment store"
        print(f"[winnow] could not read {where}: {e}", file=sys.stderr)
        return loaded
    for name, value in data.items():
        if name in RESERVED or not NAME_RE.match(name):
            continue
        if name not in os.environ:
            os.environ[name] = value
            _OWNED.add(name)
            loaded.append(name)
        elif os.environ[name] == value:
            # Already present and identical: the OS handed our own saved
            # value back (Windows). Ours to manage, nothing to load.
            _OWNED.add(name)
    return loaded


def list_vars(st=None) -> list[dict]:
    """Names only — never values. `stored` is on disk / in the registry,
    `live` is in this process, `shell` means something outside Winnow set
    it (so it wins over anything saved here)."""
    st = st or store()
    try:
        stored = st.load()
    except Exception:  # noqa: BLE001
        stored = {}
    live = {k for k in os.environ if NAME_RE.match(k)}
    return [{"name": n, "stored": n in stored, "live": n in live, "reserved": n in RESERVED,
             "shell": _foreign(n, stored)}
            for n in sorted(set(stored) | live)]


def set_var(name: str, value: str, st=None) -> str:
    name = check_name(name, for_write=True)
    value = check_value(value)
    st = st or store()
    try:
        stored = st.load()
    except Exception:  # noqa: BLE001 — an unreadable store must not block a save
        stored = {}
    if _foreign(name, stored):
        raise ValueError(f"{name} is set outside Winnow (a shell export) — that wins; change it there")
    st.set(name, value)
    os.environ[name] = value      # this run too, not only the next launch
    _OWNED.add(name)
    return name


def delete_var(name: str, st=None) -> None:
    name = check_name(name, for_write=True)
    (st or store()).delete(name)
    if name in _OWNED:            # a shell export is not ours to remove
        os.environ.pop(name, None)
        _OWNED.discard(name)


def get(name: str, default: str | None = None) -> str | None:
    """What a plugin reads: this process's environment, prefix-checked.
    Never the store directly — a shell export must win, and a value
    removed this run must be gone."""
    name = check_name(name)
    return os.environ.get(name, default)
