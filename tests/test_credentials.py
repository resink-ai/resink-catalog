"""Token storage: OS credential store on macOS and Windows, a 0600 file elsewhere, never in catalog.db or a backup."""

import os
import stat
import sys
import types

import pandas as pd
import pytest

from resink_catalog import Catalog
from resink_catalog.backup import backup
from resink_catalog.credentials import FileStore, KeyringStore, open_store


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


@pytest.mark.parametrize(("platform", "kind"), [("darwin", "macOS Keychain"), ("win32", "Windows Credential Manager")])
def test_macos_and_windows_use_the_os_store(monkeypatch, platform, kind):
    fake = FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    store = open_store(platform)
    assert isinstance(store, KeyringStore) and store.kind == kind
    store.set("device", "tok")
    assert store.get("device") == "tok" and fake.store == {("resink-catalog", "device"): "tok"}
    store.delete("device")
    store.delete("device")  # already gone: not an error
    assert store.get("device") is None


def test_linux_uses_the_private_file(monkeypatch, tmp_path):
    monkeypatch.setenv("RESINK_CATALOG_CREDENTIALS_DIR", str(tmp_path))
    store = open_store("linux")
    assert isinstance(store, FileStore) and store.path == tmp_path / "credentials.json"


def test_without_the_keyring_package_the_file_is_the_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("RESINK_CATALOG_CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "keyring", None)  # import raises ImportError
    assert isinstance(open_store("darwin"), FileStore)


def test_tokens_never_reach_catalog_db_or_a_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("RESINK_CATALOG_CREDENTIALS_DIR", str(tmp_path / "cfg"))
    home = tmp_path / "home"
    cat = Catalog(home)
    cat.write_table("t", pd.DataFrame({"a": [1]}))
    open_store("linux").set("device", "SECRET-DEVICE-TOKEN")
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
