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
    cat.close()
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
    cat.close()
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
    cat.close()
    assert main(["restore", str(tmp_path / "bk"), "--home", str(home), "--replace"]) == 0
    assert json.loads((tmp_path / "bk" / "manifest.json").read_text())["format"] == 1


# ------------------------------------------------------------------------------------------ restore safety (TASK-9)


def _edit_manifest(bk, fn):
    m = json.loads((bk / "manifest.json").read_text())
    fn(m)
    (bk / "manifest.json").write_text(json.dumps(m))


def _state(home):
    return sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_file() and p.name != ".catalog.lock")


@pytest.mark.parametrize(
    "evil",
    [
        "../victim",
        "/etc/passwd",
        "warehouse/../../victim",
        "warehouse/../x",
        "x/y",
        "warehouse",
        "warehouse/a\\b",
        "C:/evil",
        "",
    ],
)
def test_restore_refuses_manifest_paths_outside_warehouse_and_touches_nothing(cat, home, tmp_path, evil):
    bk = tmp_path / "bk"
    backup(home, bk)
    (tmp_path / "victim").write_text("do not overwrite")
    (bk / "x").mkdir()
    (bk / "x" / "y").write_text("y")
    cat.close()
    before = _state(home)
    _edit_manifest(bk, lambda m: m["files"].append({"path": evil, "size": 1, "sha256": "0" * 64}))
    with pytest.raises(BackupError, match="unsafe path|malformed"):
        restore(bk, home, replace=True)
    assert (tmp_path / "victim").read_text() == "do not overwrite" and _state(home) == before


def test_restore_refuses_duplicates_and_symlinks(cat, home, tmp_path):
    bk = tmp_path / "bk"
    m = backup(home, bk)
    cat.close()
    first = m["files"][0]
    _edit_manifest(bk, lambda mm: mm["files"].append(first))
    with pytest.raises(BackupError, match="listed twice"):
        restore(bk, home, replace=True)
    _edit_manifest(bk, lambda mm: mm["files"].pop())
    victim = bk / first["path"]
    outside = tmp_path / "outside"
    outside.write_bytes(victim.read_bytes())
    victim.unlink()
    victim.symlink_to(outside)  # same bytes, so the checksum would pass
    with pytest.raises(BackupError, match="symbolic link"):
        restore(bk, home, replace=True)


def test_a_restore_that_fails_in_the_middle_of_the_swap_puts_the_old_catalog_back(cat, home, tmp_path, monkeypatch):
    from resink_catalog import backup as backup_module

    bk = tmp_path / "bk"
    backup(home, bk)
    cat.write_table("shop.extra", pd.DataFrame({"x": [1]}))  # the live catalog now differs from the backup
    cat.close()
    before = _state(home)
    real, calls = backup_module._rename, []

    def flaky(a, b):
        calls.append((a.name, b.name))
        if len(calls) == 4:  # db aside, warehouse aside, warehouse in, then the new catalog.db: fail there
            raise OSError("disk full")
        real(a, b)

    monkeypatch.setattr(backup_module, "_rename", flaky)
    with pytest.raises(OSError, match="disk full"):
        restore(bk, home, replace=True)
    monkeypatch.undo()
    assert _state(home) == before and not list(home.glob(".restore-*")) and not list(home.glob("*.replaced-*"))
    assert sorted(t["name"] for t in Catalog(home).list_tables()) == ["shop.extra", "shop.orders"]


def test_a_manifest_that_omits_a_needed_file_is_refused_before_the_catalog_changes(cat, home, tmp_path):
    bk = tmp_path / "bk"
    backup(home, bk)
    cat.close()
    before = _state(home)
    _edit_manifest(
        bk, lambda m: m["files"].__setitem__(slice(None), [e for e in m["files"] if "metadata.json" not in e["path"]])
    )
    with pytest.raises(BackupError, match="does not contain"):
        restore(bk, home, replace=True)
    assert _state(home) == before


def test_restore_refuses_while_the_catalog_is_open_and_handles_sqlite_sidecars(cat, home, tmp_path):
    bk = tmp_path / "bk"
    backup(home, bk)
    with pytest.raises(BackupError, match="open in another process"):
        restore(bk, home, replace=True)  # `cat` is open here, as a running server would be
    assert Catalog(home).list_tables()[0]["rows"] == 4
    cat.close()
    import gc

    gc.collect()
    (home / "catalog.db-wal").write_bytes(b"stale")  # a leftover of a crashed process
    restore(bk, home, replace=True)
    assert not (home / "catalog.db-wal").exists()
    assert list(home.glob("catalog.db-wal.replaced-*"))


def test_a_running_server_blocks_restore(cat, home, tmp_path):
    """The real thing: `serve` in a subprocess holds the lock while idle."""
    import subprocess
    import sys

    bk = tmp_path / "bk"
    backup(home, bk)
    cat.close()
    proc = subprocess.Popen(
        [sys.executable, "-m", "resink_catalog.cli", "serve", "--no-browser", "--port", "0", "--home", str(home)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert "listening" in proc.stdout.readline()
        with pytest.raises(BackupError, match="open in another process"):
            restore(bk, home, replace=True)
    finally:
        proc.terminate()
        proc.wait(10)
    restore(bk, home, replace=True)  # the server is gone: allowed
