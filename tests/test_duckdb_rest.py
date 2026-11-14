"""L1 acceptance 3, DuckDB side: DuckDB's `iceberg` extension against our REST endpoint.

The extension and `httpfs` are downloaded by DuckDB on first use, so they are not a test dependency (D10): these
tests never install anything and are skipped when the extensions are not already in DuckDB's extension directory.
Install them once, by hand, with `uv run python scripts/duckdb_rest_probe.py --install`.
"""

import duckdb
import pandas as pd
import pytest

from resink_catalog import Catalog


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(config={"autoinstall_known_extensions": False, "autoload_known_extensions": False})
    for ext in ("httpfs", "iceberg"):
        try:
            con.execute(f"LOAD {ext}")
        except duckdb.Error:
            pytest.skip(f"DuckDB extension {ext!r} is not installed (needs a one-time download; see module docstring)")
    return con


def _attach(con, live_server, auth: str) -> None:
    con.execute(f"CREATE SECRET (TYPE iceberg, TOKEN '{live_server.token}')")
    con.execute(
        f"ATTACH 'local' AS ice (TYPE iceberg, ENDPOINT '{live_server.iceberg_uri}', AUTHORIZATION_TYPE '{auth}')"
    )


@pytest.fixture
def duck(live_server):
    """DuckDB attached with the documented form: a token secret and AUTHORIZATION_TYPE 'oauth2'."""
    con = _connect()
    _attach(con, live_server, "oauth2")
    return con


def test_attach_without_the_token_is_refused(live_server):
    """AUTHORIZATION_TYPE 'none' sends no token, and every route but /healthz needs one."""
    with pytest.raises(duckdb.Error, match="401"):
        _attach(_connect(), live_server, "none")


def test_duckdb_lists_and_reads_tables_written_by_the_catalog(duck, live_server):
    Catalog(live_server.home).write_table("shop.orders", pd.DataFrame({"id": [1, 2, 3], "name": list("abc")}))
    names = {
        r[0] for r in duck.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'ice'").fetchall()
    }
    assert "orders" in names
    assert duck.execute("SELECT id FROM ice.shop.orders ORDER BY id").fetchall() == [(1,), (2,), (3,)]


def test_pyiceberg_sees_what_duckdb_wrote(duck, live_server):
    cat = Catalog(live_server.home)
    cat.write_table("shop.orders", pd.DataFrame({"id": [1], "name": ["a"]}))
    duck.execute("INSERT INTO ice.shop.orders VALUES (2, 'b')")
    assert sorted(cat.read_table("shop.orders")["id"].tolist()) == [1, 2]
    assert len(cat.iceberg.load_table(("shop", "orders")).metadata.snapshots) == 2  # one new snapshot, nothing lost


def test_duckdb_sees_what_pyiceberg_wrote(duck, live_server):
    cat = Catalog(live_server.home)
    cat.write_table("shop.orders", pd.DataFrame({"id": [1], "name": ["a"]}))
    assert duck.execute("SELECT count(*) FROM ice.shop.orders").fetchone() == (1,)
    cat.write_table("shop.orders", pd.DataFrame({"id": [5, 6], "name": ["x", "y"]}), mode="append")
    assert duck.execute("SELECT count(*) FROM ice.shop.orders").fetchone() == (3,)  # DuckDB re-reads the table


def test_duckdb_creates_tables_that_pyiceberg_reads_and_that_stay_pushable(duck, live_server):
    duck.execute("CREATE SCHEMA ice.duck")
    duck.execute("CREATE TABLE ice.duck.t (a INTEGER, b VARCHAR)")
    duck.execute("INSERT INTO ice.duck.t VALUES (1, 'x'), (2, 'y')")
    cat = Catalog(live_server.home)
    assert cat.read_table("duck.t")["b"].tolist() == ["x", "y"]
    table = cat.iceberg.load_table(("duck", "t"))
    assert table.spec().is_unpartitioned()
    tasks = list(table.scan().plan_files())
    assert tasks and all(t.file.file_format.name == "PARQUET" and not t.delete_files for t in tasks)


def test_duckdb_delete_works_but_makes_the_table_unpushable(duck, live_server):
    """DuckDB deletes by writing position delete files (merge-on-read). Readers honour them; push v0 refuses them."""
    cat = Catalog(live_server.home)
    cat.write_table("shop.orders", pd.DataFrame({"id": [1, 2, 3], "name": list("abc")}))
    assert cat.push_blockers("shop.orders") == []
    duck.execute("DELETE FROM ice.shop.orders WHERE id = 2")
    assert sorted(cat.read_table("shop.orders")["id"].tolist()) == [1, 3]
    assert cat.query("select count(*) as n from shop.orders")["n"].tolist() == [2]
    assert cat.push_blockers("shop.orders") == ["delete files"]


def test_duckdb_update_is_visible_to_pyiceberg(duck, live_server):
    cat = Catalog(live_server.home)
    cat.write_table("shop.orders", pd.DataFrame({"id": [1, 2], "name": ["a", "b"]}))
    duck.execute("UPDATE ice.shop.orders SET name = 'z' WHERE id = 1")
    df = cat.read_table("shop.orders").sort_values("id")
    assert df["name"].tolist() == ["z", "b"]
