"""Per-user environment variables Winnow is allowed to manage — the home
for tokens and other secrets a plugin needs, kept OUT of the case file
and out of Winnow's own settings.

The rule is a prefix: only `WINNOW_*` names can be set, removed or read
through this module, so the Settings panel can never become a general
environment editor and a plugin can never fish `AWS_SECRET_ACCESS_KEY`
out of the process through Winnow's API. Names Winnow itself reads for
configuration (RESERVED) can be read but not changed here — they are
command-line knobs, not secrets, and changing one from the UI would move
the workspace out from under a running install.

Where a value is kept is the OS's own per-user environment, so it is
exactly as private as the account:

- Windows: `HKCU\\Environment` — the user's real environment variables,
  the ones the System Properties dialog edits. New processes see them;
  this one is updated in place.
- Everywhere else: a `NAME=value` file, `~/.config/winnow/env` (or
  `$XDG_CONFIG_HOME/winnow/env`), created 0600 in a 0700 directory.
  Loaded into the process at startup for any `WINNOW_*` name the shell
  did not already export — a real environment variable always wins.

Values are never returned by the API and never shown by the UI: the
panel lists names and where each came from, nothing more. A plugin gets
a value through `PluginRequest.env(name)`, server-side only; nothing
here is ever sent to the browser.

`WINNOW_ENV_FILE` relocates the file (tests point it at a tmp dir).
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

PREFIX = "WINNOW_"
NAME_RE = re.compile(r"^WINNOW_[A-Z0-9_]{1,56}$")
# Winnow's own configuration knobs. Readable, never editable from here.
RESERVED = frozenset({
    "WINNOW_WORKSPACE_DIR", "WINNOW_PLUGINS_DIR", "WINNOW_CASES_DIR", "WINNOW_ENV_FILE",
    "WINNOW_IDLE_EXIT_S", "WINNOW_IDLE_TICK_S", "WINNOW_NEVER_CONNECTED_EXIT_S",
})
MAX_VALUE = 8192


def check_name(name: str, *, for_write: bool = False) -> str:
    """The name, or ValueError saying what is wrong with it."""
    name = (name or "").strip()
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
            if k.startswith(PREFIX):
                out[k] = v
        return out

    def _write(self, data: dict[str, str]) -> None:
        d = self.path.parent
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        body = "".join(f"{k}={v}\n" for k, v in sorted(data.items()))
        fd, tmp = tempfile.mkstemp(prefix=".env-", dir=str(d))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with _suppress():
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
        try:
            i = 0
            while True:
                try:
                    name, value, _kind = self.reg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if str(name).startswith(PREFIX):
                    out[str(name)] = str(value)
        finally:
            self.reg.CloseKey(key)
        return out

    def set(self, name: str, value: str) -> None:
        key = self.reg.CreateKeyEx(self.reg.HKEY_CURRENT_USER, self.KEY, 0, self.reg.KEY_SET_VALUE)
        try:
            self.reg.SetValueEx(key, name, 0, self.reg.REG_SZ, value)
        finally:
            self.reg.CloseKey(key)
        self._notify()

    def delete(self, name: str) -> None:
        try:
            key = self.reg.OpenKey(self.reg.HKEY_CURRENT_USER, self.KEY, 0, self.reg.KEY_SET_VALUE)
        except OSError:
            return
        try:
            self.reg.DeleteValue(key, name)
        except OSError:
            pass
        finally:
            self.reg.CloseKey(key)
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


class _suppress:
    def __enter__(self): return self
    def __exit__(self, *a): return True


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
    """Startup: put every stored WINNOW_* value into this process's
    environment unless the shell already exported that name — the real
    environment wins, so a value pasted into the UI can never override
    one set deliberately outside. Returns the names loaded."""
    st = st or store()
    loaded = []
    try:
        data = st.load()
    except Exception as e:  # noqa: BLE001 — a bad file must never block startup
        print(f"[winnow] could not read {st.location()}: {e}", file=sys.stderr)
        return loaded
    for name, value in data.items():
        if name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return loaded


def list_vars(st=None) -> list[dict]:
    """Names only — never values. `stored` is on disk / in the registry,
    `live` is in this process (from the shell or set this run)."""
    st = st or store()
    try:
        stored = set(st.load())
    except Exception:  # noqa: BLE001
        stored = set()
    live = {k for k in os.environ if k.startswith(PREFIX)}
    return [{"name": n, "stored": n in stored, "live": n in live, "reserved": n in RESERVED}
            for n in sorted(stored | live)]


def set_var(name: str, value: str, st=None) -> str:
    name = check_name(name, for_write=True)
    value = check_value(value)
    (st or store()).set(name, value)
    os.environ[name] = value      # this run too, not only the next launch
    return name


def delete_var(name: str, st=None) -> str:
    name = check_name(name, for_write=True)
    (st or store()).delete(name)
    os.environ.pop(name, None)
    return name


def get(name: str, default: str | None = None) -> str | None:
    """What a plugin reads: this process's environment, prefix-checked.
    Never the store directly — a shell export must win, and a value
    removed this run must be gone."""
    name = check_name(name)
    return os.environ.get(name, default)
