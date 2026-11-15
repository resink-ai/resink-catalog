"""Where the push client keeps its device tokens (push protocol section 3, decision 11).

macOS Keychain and Windows Credential Manager through the `keyring` package, with the native backend chosen
explicitly. Where there is none (Linux) the program refuses, unless the user opts in to a 0600 file.
Never in `catalog.db` (users back that up and share it) and never in the data directory that `backup` copies.
"""

from __future__ import annotations

import json
import logging
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


PLAINTEXT_ENV = "RESINK_CATALOG_ALLOW_PLAINTEXT_CREDENTIALS"
log = logging.getLogger(__name__)


class NoSecureStore(Exception):
    """No operating-system credential store is usable and the plaintext file was not explicitly allowed."""


def _native_backend(platform: str):
    """The OS store's own backend, picked explicitly: never whatever backend keyring happens to have selected."""
    if platform == "darwin":
        from keyring.backends.macOS import Keyring

        return Keyring(), "macOS Keychain"
    if platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring(), "Windows Credential Manager"
    raise NoSecureStore(f"this program has no OS credential store for platform {platform!r}")


def open_store(platform: str | None = None, env: dict | None = None) -> TokenStore:
    """The OS credential store (macOS Keychain, Windows Credential Manager), chosen explicitly. If it cannot be used
    (Linux, a missing package, no usable backend) this raises NoSecureStore. Tokens go into the 0600 file in the
    user's config directory only when the user sets RESINK_CATALOG_ALLOW_PLAINTEXT_CREDENTIALS=1; that logs a
    warning, and is refused on Windows, where this code sets no access control list."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    reason = ""
    try:
        backend, kind = _native_backend(platform)
        if backend.priority <= 0:  # keyring's own "not usable here" signal (raises or returns <= 0)
            raise RuntimeError("backend reports itself unavailable")
        return KeyringStore(backend, kind)
    except Exception as e:  # ImportError, NoSecureStore, a backend that cannot start
        reason = f"{type(e).__name__}: {e}"
    if env.get(PLAINTEXT_ENV) == "1" and platform != "win32":
        log.warning(
            "storing credentials in a plaintext file (mode 0600) because %s=1; reason: %s", PLAINTEXT_ENV, reason
        )
        return FileStore()
    raise NoSecureStore(
        f"no secure credential store is available ({reason}). Set {PLAINTEXT_ENV}=1 to store tokens in a "
        "file readable only by you instead (not available on Windows)."
    )
