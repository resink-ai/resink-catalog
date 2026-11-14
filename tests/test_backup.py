"""Consistent backup and restore: the write lock, referenced files only, checksums, and a restore that verifies."""

import json
import sqlite3
import threading
import time

import pandas as pd
import pytest

from resink_catalog import Catalog
from resink_catalog.backup import BackupError, backup, referenced_files, restore, verify_backup
from resink_catalog.cli import main


@pytest.fixture
def cat(home):
    c = Catalog(home)
    c.write_table("shop.orders", pd.DataFrame({"id": [1, 2, 3], "amount": [1.5, 2.5, 3.5]}))
    c.write_table("shop.orders", pd.DataFrame({"id": [9], "amount": [9.0]}), mode="append")  # two snapshots
    c.add_tags("shop.orders", ["finance"], source="ai")
    c.write_note("shop.orders.amount", "EUR", author="user")
    (home / "token").write_text("secret-token\n")
    return c


def test_backup_copies_the_catalog_and_only_referenced_files_and_never_the_token(cat, home, tmp_path):
    orphan = home / "warehouse" / "leftover-of-an-interrupted-import.parquet"
    orphan.write_bytes(b"nobody references me")
    m = backup(home, tmp_path / "bk")
    copied = {e["path"] for e in m["files"]}
    assert "warehouse/leftover-of-an-interrupted-import.parquet" not in copied
    assert {f.resolve().relative_to((home / "warehouse").resolve()).as_posix() for f in referenced_files(cat)} == {
        p.removeprefix("warehouse/") for p in copied
    }
    assert any(p.endswith(".parquet") for p in copied) and any(p.endswith("metadata.json") for p in copied)
    assert (
        not (tmp_path / "bk" / "token").exists()
        and "secret-token" not in (tmp_path / "bk" / "manifest.json").read_text()
    )
    assert verify_backup(tmp_path / "bk")["catalog_db_sha256"] == m["catalog_db_sha256"]


def test_restore_after_the_catalog_is_destroyed_gives_the_same_tables_history_tags_and_notes(cat, home, tmp_path):
    before = cat.list_tables()
    rows_before = cat.query("select * from shop.orders order by id").to_dict("records")
    history_before = [s["snapshot_id"] for s in cat.table_info("shop.orders")["snapshots"]]
    backup(home, tmp_path / "bk")
    (home / "catalog.db").unlink()
    import shutil

    shutil.rmtree(home / "warehouse")
    restore(tmp_path / "bk", home)
    again = Catalog(home)
    assert again.list_tables() == before
    assert again.query("select * from shop.orders order by id").to_dict("records") == rows_before
    assert [s["snapshot_id"] for s in again.table_info("shop.orders")["snapshots"]] == history_before
    assert [t["tag"] for t in again.list_tags("shop.orders")] == ["finance"]
    assert [n["body"] for n in again.list_notes("shop.orders")] == ["EUR"]


def test_restore_refuses_an_existing_catalog_unless_replace_and_then_moves_it_aside(cat, home, tmp_path):
    backup(home, tmp_path / "bk")
    with pytest.raises(BackupError, match="already has a catalog"):
        restore(tmp_path / "bk", home)
    cat.write_table("shop.extra", pd.DataFrame({"x": [1]}))
    restore(tmp_path / "bk", home, replace=True)
    assert [t["name"] for t in Catalog(home).list_tables()] == ["shop.orders"]
    assert list(home.glob("catalog.db.replaced-*")) and list(home.glob("warehouse.replaced-*"))  # nothing deleted


def test_a_damaged_or_incomplete_backup_is_refused_before_anything_is_changed(cat, home, tmp_path):
    bk = tmp_path / "bk"
    m = backup(home, bk)
    victim = bk / next(e["path"] for e in m["files"] if e["path"].endswith(".parquet"))
    victim.write_bytes(victim.read_bytes()[:-1] + b"X")
    with pytest.raises(BackupError, match="checksum"):
        restore(bk, home, replace=True)
    assert Catalog(home).list_tables()[0]["rows"] == 4  # the live catalog was not touched
    (bk / "manifest.json").unlink()
    with pytest.raises(BackupError, match="not a complete backup"):
        verify_backup(bk)


def test_restore_to_a_different_location_is_refused_with_the_reason(cat, home, tmp_path):
    backup(home, tmp_path / "bk")
    with pytest.raises(BackupError, match="absolute paths"):
        restore(tmp_path / "bk", tmp_path / "elsewhere")


def test_backup_refuses_a_non_empty_target_and_one_inside_the_catalog(cat, home, tmp_path):
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x")
    with pytest.raises(BackupError, match="not empty"):
        backup(home, tmp_path / "full")
    with pytest.raises(BackupError, match="outside"):
        backup(home, home / "inside")


def test_the_backup_is_consistent_while_another_process_writes(cat, home, tmp_path, monkeypatch):
    """A writer that starts during the backup waits for the lock, so the backup holds either all or none of its rows."""
    from resink_catalog import backup as backup_module

    started, release = threading.Event(), threading.Event()
    real = backup_module.referenced_files

    def slow(c):
        started.set()
        release.wait(5)  # the lock is held while we are in here
        return real(c)

    monkeypatch.setattr(backup_module, "referenced_files", slow)
    t = threading.Thread(target=lambda: backup(home, tmp_path / "bk"))
    t.start()
    assert started.wait(10)
    writer_done = threading.Event()

    def write():
        conn = sqlite3.connect(home / "catalog.db", timeout=30)
        with conn:
            conn.execute(
                "INSERT INTO tags (table_name, tag, source, created_at) VALUES ('shop.orders','late','user','x')"
            )
        writer_done.set()

    w = threading.Thread(target=write)
    w.start()
    time.sleep(0.5)
    assert not writer_done.is_set(), "the writer must wait for the backup's write lock"
    release.set()
    t.join(30)
    w.join(30)
    assert writer_done.is_set()
    with sqlite3.connect(tmp_path / "bk" / "catalog.db") as c:  # the backup has the state before the late write
        assert [r[0] for r in c.execute("SELECT tag FROM tags")] == ["finance"]
    with sqlite3.connect(home / "catalog.db") as c:
        assert {r[0] for r in c.execute("SELECT tag FROM tags")} == {"finance", "late"}


def test_the_cli_backs_up_and_restores(cat, home, tmp_path, capsys):
    assert main(["backup", str(tmp_path / "bk"), "--home", str(home)]) == 0
    assert "backed up" in capsys.readouterr().out
    assert main(["backup", str(tmp_path / "bk"), "--home", str(home)]) == 1  # not empty
    assert "error:" in capsys.readouterr().err
    assert main(["restore", str(tmp_path / "bk"), "--home", str(home)]) == 1  # catalog exists
    assert main(["restore", str(tmp_path / "bk"), "--home", str(home), "--replace"]) == 0
    assert json.loads((tmp_path / "bk" / "manifest.json").read_text())["format"] == 1
