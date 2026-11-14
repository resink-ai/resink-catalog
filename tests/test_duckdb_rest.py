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


@pytest.fixture
def duck(live_server):
    con = _connect()
    con.execute(f"CREATE SECRET (TYPE iceberg, TOKEN '{live_server.token}')")
    con.execute(
        f"ATTACH 'local' AS ice (TYPE iceberg, ENDPOINT '{live_server.iceberg_uri}', AUTHORIZATION_TYPE 'none')"
    )
    return con


def test_duckdb_lists_and_reads_tables_written_by_the_catalog(duck, live_server):
    Catalog(live_server.home).write_table("shop.orders", pd.DataFrame({"id": [1, 2, 3], "name": list("abc")}))
    names = {
        r[0] for r in duck.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'ice'").fetchall()
    }
    assert "orders" in names
    assert duck.execute("SELECT id FROM ice.shop.orders ORDER BY id").fetchall() == [(1,), (2,), (3,)]


def test_pyiceberg_sees_what_duckdb_wrote(duck, live_server):
    Catalog(live_server.home).write_table("shop.orders", pd.DataFrame({"id": [1], "name": ["a"]}))
    duck.execute("INSERT INTO ice.shop.orders VALUES (2, 'b')")
    assert sorted(Catalog(live_server.home).read_table("shop.orders")["id"].tolist()) == [1, 2]
