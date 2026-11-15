"""Catalogs made before the rename keep their tables in `default`; opening one moves them to `main`."""

import logging
import sqlite3

import pandas as pd
import pyarrow as pa

from resink_catalog.catalog import Catalog
from resink_catalog.home import init_db
from resink_catalog.store import open_iceberg


def _old_catalog(home):
    """A catalog as L1 to L3 made it: tables in `default`, with a source row, a tag and notes pointing at them."""
    init_db(home)
    ice = open_iceberg(home)
    ice.create_namespace("default")
    ice.create_table(("default", "t"), schema=pa.schema([("a", pa.int64())])).append(pa.table({"a": [1, 2, 3]}))
    ice.create_namespace("sales")
    ice.create_table(("sales", "x"), schema=pa.schema([("b", pa.int64())])).append(pa.table({"b": [9]}))
    with sqlite3.connect(home / "catalog.db") as c:
        c.execute(
            "INSERT INTO data_sources (path, format, target_table, status) VALUES ('/f.csv','csv','default.t','imported')"
        )
        c.execute("INSERT INTO tags (table_name, tag, source, created_at) VALUES ('default.t','finance','ai','now')")
        c.execute("INSERT INTO notes (target, body, author, created_at) VALUES ('default.t','table note','ai','now')")
        c.execute(
            "INSERT INTO notes (target, body, author, created_at) VALUES ('default.t.a','column note','user','now')"
        )
        c.execute("INSERT INTO notes (target, body, author, created_at) VALUES ('sales.x','other','user','now')")


def test_default_tables_move_to_main_with_their_annotations(tmp_path, caplog):
    _old_catalog(tmp_path)
    with caplog.at_level(logging.INFO, logger="resink_catalog.catalog"):
        cat = Catalog(tmp_path)
    assert cat.migrated == ["main.t"] and "main.t" in caplog.text
    assert [t["name"] for t in cat.list_tables()] == ["main.t", "sales.x"]
    assert ("default",) not in cat.iceberg.list_namespaces()
    assert cat.query("select sum(a) as s from t")["s"].tolist() == [6]  # data files are still readable
    info = cat.table_info("main.t")
    assert [t["tag"] for t in info["tags"]] == ["finance"]
    assert sorted(n["target"] for n in info["notes"]) == ["main.t", "main.t.a"]
    assert [n["body"] for n in cat.list_notes("sales.x")] == ["other"]  # other namespaces untouched
    with sqlite3.connect(tmp_path / "catalog.db") as c:
        assert c.execute("SELECT target_table FROM data_sources").fetchall() == [("main.t",)]


def test_migration_runs_once_and_never_overwrites_an_existing_main(tmp_path):
    _old_catalog(tmp_path)
    assert Catalog(tmp_path).migrated == ["main.t"]
    assert Catalog(tmp_path).migrated == []  # second start: nothing to do
    # both namespaces present (someone made main by hand): leave everything alone
    other = tmp_path / "other"
    other.mkdir()
    _old_catalog(other)
    open_iceberg(other).create_namespace("main")
    cat = Catalog(other)
    assert cat.migrated == [] and [t["name"] for t in cat.list_tables()] == ["default.t", "sales.x"]


def test_a_new_catalog_uses_main(tmp_path):
    cat = Catalog(tmp_path)
    cat.write_table("t", pd.DataFrame({"a": [1]}))
    assert [t["name"] for t in cat.list_tables()] == ["main.t"] and cat.migrated == []
