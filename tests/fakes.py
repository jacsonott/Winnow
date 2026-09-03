"""Test doubles shared across modules (tests/ has no __init__, so pytest
puts this directory on sys.path and `from fakes import ...` works)."""

from __future__ import annotations


class FakeReg:
    """Just enough of winreg's shape for WindowsAssoc and RegistryEnvStore:
    a dict of key-path → {value_name: value}, with the constants and the
    handle dance collapsed away."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = KEY_SET_VALUE = 0
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self):
        self.keys: dict[str, dict] = {}
        self.kinds: dict[tuple[str, str], int] = {}

    class _H:
        def __init__(self, path): self.path = path

    def CreateKeyEx(self, root, path, _res, _access):
        # Real CreateKeyEx creates every intermediate key too — without
        # this, _delete_tree spins forever on a parent it can list but
        # never open.
        parts = path.split("\\")
        for i in range(1, len(parts) + 1):
            self.keys.setdefault("\\".join(parts[:i]), {})
        return self._H(path)

    def OpenKey(self, root, path, _res, _access):
        if path not in self.keys:
            raise OSError(2, "no such key")
        return self._H(path)

    def CloseKey(self, h): pass

    def SetValueEx(self, h, name, _res, kind, value):
        self.keys[h.path][name] = value
        self.kinds[(h.path, name)] = kind

    def QueryValueEx(self, h, name):
        if name not in self.keys[h.path]:
            raise OSError(2, "no such value")
        return (self.keys[h.path][name], self.kinds.get((h.path, name), self.REG_SZ))

    def DeleteValue(self, h, name):
        if name not in self.keys[h.path]:
            raise OSError(2, "no such value")
        del self.keys[h.path][name]

    def DeleteKey(self, root, path):
        if path not in self.keys or any(k.startswith(path + "\\") for k in self.keys):
            raise OSError(2, "no such key or not empty")
        del self.keys[path]

    def EnumKey(self, h, i):
        subs = sorted({k[len(h.path) + 1:].split("\\")[0]
                       for k in self.keys if k.startswith(h.path + "\\")})
        if i >= len(subs):
            raise OSError(22, "no more data")
        return subs[i]

    def EnumValue(self, h, i):
        items = list(self.keys[h.path].items())
        if i >= len(items):
            raise OSError(22, "no more data")
        name, value = items[i]
        return name, value, self.kinds.get((h.path, name), self.REG_SZ)
