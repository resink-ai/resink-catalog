"""Consistent backup and restore: the write lock, referenced files only, checksums, and a restore that verifies."""

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

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
    assert list(home.glob(".restore-replaced-*/catalog.db")) and list(
        home.glob(".restore-replaced-*/warehouse")
    )  # nothing deleted


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
    assert _state(home) == before and not list(home.glob(".restore-*")) and not list(home.glob(".restore-replaced-*"))
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
    assert list(home.glob(".restore-replaced-*/catalog.db-wal"))


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


# ------------------------------------------------------------------------------------------ round 2 (TASK-11)


def test_restoring_an_empty_catalog_works(home, tmp_path):
    Catalog(home).close()  # a catalog without any table: the manifest has no files
    m = backup(home, tmp_path / "bk")
    assert m["files"] == []
    restore(tmp_path / "bk", home, replace=True)
    assert Catalog(home).list_tables() == []


def test_the_staged_database_is_checked_against_the_manifest_checksum_before_it_is_opened(
    cat, home, tmp_path, monkeypatch
):
    from resink_catalog import backup as backup_module

    bk = tmp_path / "bk"
    backup(home, bk)
    cat.close()
    (bk / "catalog.db").write_bytes(b"not the database the manifest describes")  # changed after verification...
    monkeypatch.setattr(backup_module, "verify_backup", lambda src: json.loads((bk / "manifest.json").read_text()))
    before = _state(home)
    with pytest.raises(BackupError, match="catalog.db does not match its checksum"):
        restore(bk, home, replace=True)  # ...so staging itself must catch it
    assert _state(home) == before and not list(home.glob(".restore-*"))


def test_recovery_names_are_unique_so_two_replacements_in_one_second_keep_both_old_catalogs(cat, home, tmp_path):
    bk = tmp_path / "bk"
    backup(home, bk)
    cat.close()
    for i in range(2):  # no time stamp in the names: nothing to collide, whatever the clock says
        c = Catalog(home)
        c.add_tags("shop.orders", [f"generation-{i}"], source="user")
        c.close()
        restore(bk, home, replace=True)
    dirs = sorted(home.glob(".restore-replaced-*"))
    assert len(dirs) == 2 and all((d / "catalog.db").is_file() and (d / "warehouse").is_dir() for d in dirs)
    tags = set()
    for d in dirs:
        with sqlite3.connect(d / "catalog.db") as c:
            tags |= {r[0] for r in c.execute("SELECT tag FROM tags")}
    assert {"generation-0", "generation-1"} <= tags  # the first old catalog was not overwritten by the second


CRASH_SCRIPT = """
import os, sys
from pathlib import Path
from resink_catalog import backup as b
bk, home, point = sys.argv[1], sys.argv[2], sys.argv[3]
count = {"n": 0}
real = b._rename
def rename(a, c):
    count["n"] += 1
    if point == f"before-rename-{count['n']}":
        os._exit(9)          # killed: no rollback code runs
    real(a, c)
    if point == f"after-rename-{count['n']}":
        os._exit(9)
b._rename = rename
real_marker = b._commit_marker
def marker(h, j):
    if point == "before-commit-marker":
        os._exit(9)
    real_marker(h, j)
    if point == "after-commit-marker":
        os._exit(9)
b._commit_marker = marker
b.restore(bk, home, replace=True)
"""


@pytest.mark.parametrize(
    "point",
    [f"{when}-rename-{n}" for n in (1, 2, 3, 4) for when in ("before", "after")]
    + ["before-commit-marker", "after-commit-marker"],
)
def test_a_restore_killed_at_any_step_is_undone_or_finished_at_the_next_start(cat, home, tmp_path, point):
    """A real process is killed with os._exit between the renames (no `except` block runs). The next Catalog() must find
    either the old catalog or the restored one, never an empty or mixed one."""
    import subprocess
    import sys

    bk = tmp_path / "bk"
    backup(home, bk)
    cat.write_table("shop.extra", pd.DataFrame({"x": [1]}))  # the live catalog differs from the backup
    cat.close()
    import gc

    gc.collect()
    proc = subprocess.run([sys.executable, "-c", CRASH_SCRIPT, str(bk), str(home), point], capture_output=True)
    assert proc.returncode == 9, proc.stderr.decode()[-400:]
    assert (home / ".restore-journal.json").exists()  # the journal was durable before the first rename
    names = sorted(t["name"] for t in Catalog(home).list_tables())  # recovery runs inside Catalog()
    if point == "after-commit-marker":
        assert names == ["shop.orders"]  # the restore had been committed: finished
    else:
        assert names == ["shop.extra", "shop.orders"]  # undone: the old catalog, tables and data intact
        c = Catalog(home)
        assert c.query("select count(*) as n from shop.extra")["n"][0] == 1
        c.close()
    assert not (home / ".restore-journal.json").exists() and not list(home.glob(".restore-stage-*"))
    assert point == "after-commit-marker" or not list(home.glob(".restore-replaced-*"))


def test_recovery_is_idempotent_and_survives_being_killed_itself(cat, home, tmp_path):
    from resink_catalog.restore_journal import recover

    bk = tmp_path / "bk"
    backup(home, bk)
    cat.close()
    import gc
    import subprocess
    import sys

    gc.collect()
    subprocess.run([sys.executable, "-c", CRASH_SCRIPT, str(bk), str(home), "after-rename-3"], capture_output=True)
    assert recover(home) == "rolled back" and recover(home) is None  # second run: nothing to do
    assert [t["name"] for t in Catalog(home).list_tables()] == ["shop.orders"]


def test_a_journal_that_names_paths_outside_home_is_refused(home, tmp_path):
    from resink_catalog.restore_journal import RestoreRecoveryError, recover, write_journal

    home.mkdir(parents=True)
    write_journal(
        home,
        {
            "format": 1,
            "state": "swapping",
            "stage": str(home / ".s"),
            "replaced": str(home / ".r"),
            "moves": [[str(tmp_path / "victim"), str(home / "x")]],
        },
    )
    with pytest.raises(RestoreRecoveryError, match="outside"):
        recover(home)


# ------------------------------------------------------------------------------------------ round 3 (TASK-13)

RECOVERY_CRASH = """
import os, sys
from pathlib import Path
from resink_catalog import restore_journal as rj
home, point = sys.argv[1], sys.argv[2]
def hook(p):
    if p == point:
        os._exit(9)
rj._hook = hook
print(rj.recover(Path(home)))
"""


def _killed_restore(cat, home, tmp_path, point="after-rename-3"):
    import gc
    import subprocess
    import sys

    bk = tmp_path / "bk"
    backup(home, bk)
    cat.write_table("shop.extra", pd.DataFrame({"x": [1]}))  # the live catalog differs from the backup
    cat.close()
    gc.collect()
    proc = subprocess.run([sys.executable, "-c", CRASH_SCRIPT, str(bk), str(home), point], capture_output=True)
    assert proc.returncode == 9, proc.stderr.decode()[-300:]


@pytest.mark.parametrize(
    "point",
    [f"undo-rename-{n}-{when}" for n in (1, 2, 3) for when in ("before", "after")]
    + ["rolled-back-marker-before", "rolled-back-marker-after", "cleanup-after-stage", "journal-unlink-before"],
)
def test_recovery_killed_at_any_point_is_resumed_and_the_old_catalog_survives(cat, home, tmp_path, point):
    """Round 3: recovery itself is killed (os._exit). The journal stays until its recovery completed, and a second
    recovery ends with the old catalog intact, whatever the first one managed to do."""
    import subprocess
    import sys

    _killed_restore(cat, home, tmp_path)
    proc = subprocess.run([sys.executable, "-c", RECOVERY_CRASH, str(home), point], capture_output=True)
    assert proc.returncode == 9, (point, proc.stdout, proc.stderr.decode()[-300:])
    assert (home / ".restore-journal.json").exists()  # not removed before the recovery completed
    c = Catalog(home)  # runs recovery again
    assert sorted(t["name"] for t in c.list_tables()) == ["shop.extra", "shop.orders"]
    assert c.query("select count(*) as n from shop.extra")["n"][0] == 1
    c.close()
    assert not (home / ".restore-journal.json").exists() and not list(home.glob(".restore-stage-*"))
    assert not list(home.glob(".restore-replaced-*"))


def test_a_rolled_back_journal_is_cleanup_only_it_never_replays_the_moves(cat, home, tmp_path):
    """The P1 of round 3: old files are back in place, the journal still lists the (reversed) moves. If recovery read
    those as forward moves it would carry the restored old catalog back into the staging directory."""
    import subprocess
    import sys

    from resink_catalog.restore_journal import read_journal

    _killed_restore(cat, home, tmp_path)
    subprocess.run([sys.executable, "-c", RECOVERY_CRASH, str(home), "rolled-back-marker-after"], capture_output=True)
    j = read_journal(home)
    assert j["state"] == "rolled_back" and any(m[1].endswith("catalog.db") for m in j["moves"])
    assert (home / "catalog.db").exists() and (home / "warehouse").is_dir()  # the old catalog is in place
    shutil_stage = list(home.glob(".restore-stage-*"))
    assert shutil_stage  # staging still there: that is exactly the state in which a replay would hurt
    from resink_catalog.restore_journal import recover

    assert recover(home) == "rolled back"
    assert (home / "catalog.db").exists() and (home / "warehouse").is_dir()
    assert sorted(t["name"] for t in Catalog(home).list_tables()) == ["shop.extra", "shop.orders"]


def test_a_failed_recovery_keeps_its_journal_and_restore_does_not_delete_it(cat, home, tmp_path, monkeypatch):
    from resink_catalog import restore_journal as rj

    _killed_restore(cat, home, tmp_path)
    bk = tmp_path / "bk2"
    shutil_home = home  # a second backup of the same location, to call restore with
    assert (home / ".restore-journal.json").exists()
    before = (home / ".restore-journal.json").read_text()
    real = rj.durable_rename

    def failing(a, b):
        raise OSError("I/O error during rollback")

    monkeypatch.setattr(rj, "durable_rename", failing)
    with pytest.raises(OSError, match="I/O error"):
        restore(tmp_path / "bk", shutil_home, replace=True)  # its initial recover() fails
    assert (home / ".restore-journal.json").read_text() == before  # the instructions were not thrown away
    with pytest.raises(OSError, match="I/O error"):
        Catalog(home)  # nor by a normal start
    assert (home / ".restore-journal.json").exists()
    monkeypatch.setattr(rj, "durable_rename", real)
    assert sorted(t["name"] for t in Catalog(home).list_tables()) == ["shop.extra", "shop.orders"]  # and it recovers
    del bk


def test_a_failing_rollback_inside_restore_keeps_the_journal(cat, home, tmp_path, monkeypatch):
    """The swap fails (final check), and then the undo itself fails: the journal must stay for the next start."""
    from resink_catalog import backup as backup_module
    from resink_catalog import restore_journal as rj

    bk = tmp_path / "bk"
    backup(home, bk)
    cat.write_table("shop.extra", pd.DataFrame({"x": [1]}))
    cat.close()
    monkeypatch.setattr(backup_module, "referenced_files", lambda c: [home / "warehouse" / "nope"])  # final check fails
    real = rj.durable_rename
    state = {"n": 0}

    def flaky(a, b):
        state["n"] += 1
        if state["n"] > 1:  # the forward renames use backup.py's own reference; the 2nd undo rename fails
            raise OSError("disk went away")
        real(a, b)

    monkeypatch.setattr(rj, "durable_rename", flaky)
    with pytest.raises(OSError, match="disk went away"):
        restore(bk, home, replace=True)
    assert (home / ".restore-journal.json").exists()
    monkeypatch.undo()
    assert sorted(t["name"] for t in Catalog(home).list_tables()) == ["shop.extra", "shop.orders"]


def test_the_durability_order_staged_data_then_journal_then_renames_with_both_parents_then_marker(
    cat, home, tmp_path, monkeypatch
):
    from resink_catalog import backup as backup_module
    from resink_catalog import restore_journal as rj

    bk = tmp_path / "bk"
    backup(home, bk)
    cat.close()
    events = []
    for name in ("fsync_tree", "fsync_dir"):
        real = getattr(rj, name)
        monkeypatch.setattr(rj, name, lambda *a, _r=real, _n=name: (events.append((_n, Path(a[0]).name)), _r(*a))[1])
    real_rename, real_write = rj.durable_rename, rj.write_journal
    monkeypatch.setattr(
        rj, "durable_rename", lambda a, b: (events.append(("rename", a.name, b.name)), real_rename(a, b))[1]
    )
    monkeypatch.setattr(rj, "write_journal", lambda h, d: (events.append(("journal", d["state"])), real_write(h, d))[1])
    monkeypatch.setattr(backup_module, "fsync_tree", rj.fsync_tree)
    monkeypatch.setattr(backup_module, "fsync_dir", rj.fsync_dir)
    monkeypatch.setattr(backup_module, "write_journal", rj.write_journal)
    monkeypatch.setattr(backup_module, "durable_rename", rj.durable_rename)
    restore(bk, home, replace=True)
    kinds = [e[0] for e in events]
    first_journal = kinds.index("journal")
    assert kinds.index("fsync_tree") < first_journal and events[first_journal] == ("journal", "swapping")
    renames = [i for i, e in enumerate(events) if e[0] == "rename"]
    assert len(renames) == 4 and renames[0] > first_journal  # no rename before the journal is durable
    assert events[kinds.index("journal", first_journal + 1)] == ("journal", "committed")
    assert kinds.index("journal", first_journal + 1) > renames[-1]  # the marker comes after every rename
    # POSIX: each rename is followed by an fsync of its parent directory (both, when they differ) before the next
    # step. Windows has no directory fsync; there the rename itself is write-through (test_windows_paths).
    if sys.platform != "win32":
        for i in renames:
            assert events[i + 1][0] == "fsync_dir"
    assert not list(home.glob(".restore-journal.json"))  # removed last


# ------------------------------------------------------------------------------------------ round 4 (TASK-14)


def test_an_uncertain_commit_marker_is_never_rolled_back_in_process(cat, home, tmp_path, monkeypatch):
    """The marker replaces the journal with `committed`, then the directory fsync fails. Our own record still said
    `swapping`; the old code started an undo under a journal that already said `committed`. Now nothing is undone
    here: the journal stays, the restored catalog stays, and recovery decides from the disk."""
    import subprocess
    import sys

    from resink_catalog import backup as backup_module
    from resink_catalog.restore_journal import read_journal

    bk = tmp_path / "bk"
    backup(home, bk)
    cat.write_table("shop.extra", pd.DataFrame({"x": [1]}))  # the live catalog differs from the backup
    cat.close()
    real = backup_module.write_journal

    def replace_then_fail(h, data):
        real(h, data)  # the journal is replaced durably...
        if data["state"] == "committed":
            raise OSError("fsync of the directory failed")  # ...and then the call reports an error

    monkeypatch.setattr(backup_module, "write_journal", replace_then_fail)
    with pytest.raises(OSError, match="fsync of the directory failed"):
        restore(bk, home, replace=True)
    monkeypatch.undo()
    assert read_journal(home)["state"] == "committed"  # kept, and no undo was started
    assert list(home.glob(".restore-stage-*")) and (home / "catalog.db").exists()
    # a recovery that is itself interrupted part-way through its cleanup, then the next start
    proc = subprocess.run([sys.executable, "-c", RECOVERY_CRASH, str(home), "cleanup-after-stage"], capture_output=True)
    assert proc.returncode == 9
    c = Catalog(home)
    assert [t["name"] for t in c.list_tables()] == ["shop.orders"]  # complete: the restored catalog
    assert c.query("select count(*) as n from shop.orders")["n"][0] == 4
    c.close()
    assert not (home / ".restore-journal.json").exists() and not list(home.glob(".restore-stage-*"))
    assert list(home.glob(".restore-replaced-*/catalog.db"))  # the old catalog is kept, as for any committed restore


# ------------------------------------------------------------------ Windows locations (simulated: CI runs the real thing)


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("file:///C:/Users/x/warehouse/a/b.json", "C:/Users/x/warehouse/a/b.json"),  # what an old pyiceberg stored
        ("C:\\Users\\x\\warehouse\\a\\b.json", "C:\\Users\\x\\warehouse\\a\\b.json"),  # plain drive path
        ("C:/Users/x/warehouse/a b.json", "C:/Users/x/warehouse/a b.json"),
        ("file:///C:/Users/x/a%20b.json", "C:/Users/x/a b.json"),
        ("file:///home/x/warehouse/a.json", "/home/x/warehouse/a.json"),
    ],
)
def test_local_path_reads_windows_locations_on_windows(location, expected):
    from resink_catalog.backup import _local_path

    assert str(_local_path(location, windows=True)).replace("\\", "/") == expected.replace("\\", "/")


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("file:///C:/warehouse/a.json", "/C:/warehouse/a.json"),  # an absolute path whose first directory is `C:`
        ("file:///home/x/warehouse/a%20b.json", "/home/x/warehouse/a b.json"),
        ("/home/x/warehouse/a.json", "/home/x/warehouse/a.json"),
    ],
)
def test_local_path_keeps_posix_semantics_on_posix(location, expected):
    from resink_catalog.backup import _local_path

    assert _local_path(location, windows=False).as_posix() == expected


def test_local_path_refuses_remote_locations():
    from resink_catalog.backup import BackupError, _local_path

    with pytest.raises(BackupError):
        _local_path("s3://bucket/key")
