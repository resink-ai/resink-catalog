"""Where the push client keeps its device tokens (push protocol section 3, decision 11).

macOS Keychain and Windows Credential Manager through the `keyring` package; on Linux a file with mode 0600 in the
user's config directory. Never in `catalog.db` (users back that up and share it) and never in the data directory
that `backup` copies.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Protocol

SERVICE = "resink-catalog"
DIR_ENV = "RESINK_CATALOG_CREDENTIALS_DIR"


class TokenStore(Protocol):
    kind: str

    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


def credentials_dir() -> Path:
    env = os.environ.get(DIR_ENV)
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "resink-catalog"


class FileStore:
    """A JSON file readable only by the user (0600) in a directory only the user can enter (0700)."""

    kind = "file"

    def __init__(self, directory: Path | None = None):
        self.path = (directory or credentials_dir()) / "credentials.json"

    def _read(self) -> dict[str, str]:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def get(self, name: str) -> str | None:
        return self._read().get(name)

    def set(self, name: str, value: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            self.path.parent.chmod(0o700)
        data = {**self._read(), name: value}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".credentials-")  # mkstemp creates the file as 0600
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def delete(self, name: str) -> None:
        data = self._read()
        if data.pop(name, None) is not None:
            if data:
                self.path.write_text(json.dumps(data))
            else:
                self.path.unlink()


class KeyringStore:
    """The operating system's credential store (Keychain, Credential Manager)."""

    def __init__(self, keyring, kind: str):
        self._k, self.kind = keyring, kind

    def get(self, name: str) -> str | None:
        return self._k.get_password(SERVICE, name)

    def set(self, name: str, value: str) -> None:
        self._k.set_password(SERVICE, name, value)

    def delete(self, name: str) -> None:
        try:
            self._k.delete_password(SERVICE, name)
        except Exception as e:  # keyring.errors.PasswordDeleteError: nothing stored under that name
            if type(e).__name__ != "PasswordDeleteError":
                raise


def open_store(platform: str | None = None) -> TokenStore:
    """The OS credential store on macOS and Windows, the 0600 file elsewhere (and when the store is unusable)."""
    platform = platform or sys.platform
    kind = {"darwin": "macOS Keychain", "win32": "Windows Credential Manager"}.get(platform)
    if kind:
        try:
            import keyring

            return KeyringStore(keyring, kind)
        except ImportError:
            pass  # the dependency is installed on these platforms; if it is not, the file is the safe fallback
    return FileStore()
