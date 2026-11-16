"""Token storage: the OS credential store, failing closed; a 0600 file only by explicit opt-in; never in catalog.db or a backup."""

import os
import stat
import sys
import types

import pandas as pd
import pytest

from resink_catalog import Catalog
from resink_catalog.backup import backup
from resink_catalog.credentials import FileStore, KeyringStore, NoSecureStore, open_store


def test_file_store_round_trip_with_private_permissions(tmp_path):
    s = FileStore(tmp_path / "cfg")
    assert s.get("device") is None
    s.set("device", "tok-1")
    s.set("refresh", "tok-2")
    assert (s.get("device"), s.get("refresh")) == ("tok-1", "tok-2")
    if os.name == "posix":
        assert stat.S_IMODE(s.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(s.path.parent.stat().st_mode) == 0o700
    s.set("device", "tok-3")  # replaced atomically, still private
    assert s.get("device") == "tok-3"
    if os.name == "posix":
        assert stat.S_IMODE(s.path.stat().st_mode) == 0o600
    s.delete("device")
    s.delete("nothing")  # deleting what is not there is fine
    assert s.get("device") is None and s.get("refresh") == "tok-2"
    s.delete("refresh")
    assert not s.path.exists()


class FakeKeyring(types.SimpleNamespace):
    def __init__(self):
        super().__init__(store={})

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.store:
            raise type("PasswordDeleteError", (Exception,), {})("not found")
        del self.store[(service, name)]


def fake_backend_modules(monkeypatch, priority=5):
    """keyring.backends.macOS / Windows do not import on Linux; stand-ins prove the explicit choice."""
    backend = FakeKeyring()
    backend.priority = priority
    for mod, cls in (("keyring.backends.macOS", "Keyring"), ("keyring.backends.Windows", "WinVaultKeyring")):
        monkeypatch.setitem(sys.modules, mod, types.SimpleNamespace(**{cls: lambda: backend}))
    return backend


@pytest.mark.parametrize(("platform", "kind"), [("darwin", "macOS Keychain"), ("win32", "Windows Credential Manager")])
def test_macos_and_windows_use_their_native_store_chosen_explicitly(monkeypatch, platform, kind):
    fake = fake_backend_modules(monkeypatch)
    store = open_store(platform, env={})
    assert isinstance(store, KeyringStore) and store.kind == kind
    store.set("device", "tok")
    assert store.get("device") == "tok" and fake.store == {("resink-catalog", "device"): "tok"}
    store.delete("device")
    store.delete("device")  # already gone: not an error
    assert store.get("device") is None


def test_an_unusable_native_store_fails_closed_even_if_keyring_has_another_backend(monkeypatch):
    fake_backend_modules(monkeypatch, priority=0)  # the native backend says it cannot work here
    with pytest.raises(NoSecureStore, match="no secure credential store"):
        open_store("darwin", env={})
    monkeypatch.setitem(sys.modules, "keyring.backends.macOS", None)  # package missing
    with pytest.raises(NoSecureStore):
        open_store("darwin", env={})


def test_linux_uses_a_private_file_by_default_and_warns_once(monkeypatch, tmp_path, caplog):
    """Decision 11: no native store on Linux, so a 0600 file in a 0700 directory, without any opt-in."""
    import resink_catalog.credentials as credentials

    monkeypatch.setattr(credentials, "_warned", False)
    monkeypatch.setenv("RESINK_CATALOG_CREDENTIALS_DIR", str(tmp_path / "cfg"))
    with caplog.at_level("WARNING"):
        store = open_store("linux", env={})
        open_store("linux", env={})
    assert isinstance(store, FileStore) and store.path == tmp_path / "cfg" / "credentials.json"
    assert caplog.text.count("mode 0600") == 1
    store.set("device", "t")
    if os.name == "posix":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_macos_with_an_unusable_store_refuses_unless_the_file_is_opted_in(monkeypatch, tmp_path):
    monkeypatch.setenv("RESINK_CATALOG_CREDENTIALS_DIR", str(tmp_path))
    fake_backend_modules(monkeypatch, priority=0)
    with pytest.raises(NoSecureStore, match="RESINK_CATALOG_ALLOW_PLAINTEXT_CREDENTIALS"):
        open_store("darwin", env={})
    assert not list(tmp_path.iterdir())  # nothing was written
    assert isinstance(open_store("darwin", env={"RESINK_CATALOG_ALLOW_PLAINTEXT_CREDENTIALS": "1"}), FileStore)


def test_windows_never_gets_the_plaintext_file(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring.backends.Windows", None)
    with pytest.raises(NoSecureStore):
        open_store("win32", env={"RESINK_CATALOG_ALLOW_PLAINTEXT_CREDENTIALS": "1"})  # no ACL is set by this code


def test_tokens_never_reach_catalog_db_or_a_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("RESINK_CATALOG_CREDENTIALS_DIR", str(tmp_path / "cfg"))
    home = tmp_path / "home"
    cat = Catalog(home)
    cat.write_table("t", pd.DataFrame({"a": [1]}))
    open_store("linux", env={}).set("device", "SECRET-DEVICE-TOKEN")
    backup(home, tmp_path / "bk")
    for f in [home / "catalog.db", *(tmp_path / "bk").rglob("*")]:
        if f.is_file():
            assert b"SECRET-DEVICE-TOKEN" not in f.read_bytes(), f


@pytest.mark.skipif(sys.platform not in ("darwin", "win32"), reason="needs the real OS credential store (CI matrix)")
def test_the_real_os_store_round_trips():
    store = open_store()
    assert isinstance(store, KeyringStore)
    store.set("resink-catalog-test", "value")
    try:
        assert store.get("resink-catalog-test") == "value"
    finally:
        store.delete("resink-catalog-test")
    assert store.get("resink-catalog-test") is None
