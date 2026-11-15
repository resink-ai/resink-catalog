"""Leftovers of interrupted imports are removed at start; referenced and young files never are."""

import os
import time

import pandas as pd

from resink_catalog import Catalog
from resink_catalog.cli import main
from resink_catalog.maintenance import CleanupRefused, cleanup, remove_orphans


def _age(path, hours):
    t = time.time() - hours * 3600
    os.utime(path, (t, t))


def test_only_old_unreferenced_files_are_removed(home):
    cat = Catalog(home)
    cat.write_table("shop.orders", pd.DataFrame({"id": [1, 2]}))
    data = next((home / "warehouse").rglob("*.parquet"))
    old = data.parent / "dead-interrupted-import.parquet"
    old.write_bytes(b"x")
    young = data.parent / "maybe-another-process-is-writing-this.parquet"
    young.write_bytes(b"y")
    _age(old, 48)
    nested = home / "warehouse" / "shop" / "gone" / "data"
    nested.mkdir(parents=True)
    (nested / "z.parquet").write_bytes(b"z")
    _age(nested / "z.parquet", 48)
    assert set(remove_orphans(cat, dry_run=True)) == {old, nested / "z.parquet"} and old.exists()
    assert set(remove_orphans(cat)) == {old, nested / "z.parquet"}
    assert not old.exists() and young.exists() and data.exists() and not (home / "warehouse/shop/gone").exists()
    assert cat.query("select count(*) as n from shop.orders")["n"][0] == 2  # the table is intact


def test_every_snapshot_file_survives(home):
    cat = Catalog(home)
    cat.write_table("t", pd.DataFrame({"a": [1]}))
    cat.write_table("t", pd.DataFrame({"a": [2]}), mode="append")
    for f in (home / "warehouse").rglob("*"):
        if f.is_file():
            _age(f, 100)  # everything is old; only referenced files may stay
    assert remove_orphans(cat) == []
    assert cat.query("select count(*) as n from t")["n"][0] == 2


def test_the_cli_cleanup_and_serve_startup(home, capsys):
    c = Catalog(home)
    c.write_table("t", pd.DataFrame({"a": [1]}))
    c.close()
    junk = next((home / "warehouse").rglob("*.parquet")).parent / "junk.parquet"
    junk.write_bytes(b"j")
    _age(junk, 30)
    assert main(["cleanup", "--home", str(home), "--dry-run"]) == 0
    assert "would remove" in capsys.readouterr().out and junk.exists()
    assert main(["cleanup", "--home", str(home)]) == 0 and not junk.exists()


def test_cleanup_refuses_while_any_catalog_is_open_so_an_import_in_progress_loses_nothing(home):
    """Whatever the age: with the catalog open (an import may be between creating files and committing them) nothing is
    deleted, not even empty directories; once everyone has closed, age 0 is safe."""
    import pytest

    cat = Catalog(home)
    cat.write_table("t", pd.DataFrame({"a": [1]}))
    pending = home / "warehouse" / "main" / "t_next" / "data"
    pending.mkdir(parents=True)  # what an import creates first
    (pending / "part.parquet").write_bytes(b"written, not yet committed")
    with pytest.raises(CleanupRefused, match="open in another process"):
        cleanup(home, min_age_seconds=0)
    assert (pending / "part.parquet").exists() and pending.is_dir()
    cat.close()
    assert [p.name for p in cleanup(home, min_age_seconds=0)] == ["part.parquet"]
    assert Catalog(home).query("select count(*) as n from t")["n"][0] == 1


def test_a_running_server_blocks_cleanup(home):
    import subprocess
    import sys

    import pytest

    Catalog(home).close()
    proc = subprocess.Popen(
        [sys.executable, "-m", "resink_catalog.cli", "serve", "--no-browser", "--port", "0", "--home", str(home)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert "listening" in proc.stdout.readline()
        with pytest.raises(CleanupRefused):
            cleanup(home, 0)
        assert main(["cleanup", "--home", str(home), "--min-age-hours", "0"]) == 1
    finally:
        proc.terminate()
        proc.wait(10)
    assert main(["cleanup", "--home", str(home), "--min-age-hours", "0"]) == 0
