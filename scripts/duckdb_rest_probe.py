"""Measure what DuckDB's iceberg extension can do against the local Iceberg REST endpoint.

    uv run python scripts/duckdb_rest_probe.py --install   # once: downloads the `httpfs` and `iceberg` extensions
    uv run python scripts/duckdb_rest_probe.py             # prints one line per operation: works or the error

Not part of the test suite: the download needs the network (D10).
"""

import socket
import sys
import tempfile
import threading
import time

import duckdb
import pandas as pd
import uvicorn

from resink_catalog import Catalog
from resink_catalog.app import create_app

TOKEN = "probe-token"


def main(install: bool) -> None:
    con = duckdb.connect(config={"autoinstall_known_extensions": False, "autoload_known_extensions": False})
    for ext in ("httpfs", "avro", "iceberg"):
        if install:
            con.execute(f"INSTALL {ext}")
        con.execute(f"LOAD {ext}")
    home = tempfile.mkdtemp()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(TOKEN, home), log_level="warning"))
    threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    cat = Catalog(home)
    cat.write_table("shop.orders", pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]}))

    def attempt(label, fn):
        try:
            print(f"works   {label}: {fn()}")
        except Exception as e:
            print(f"FAILS   {label}: {str(e).splitlines()[0][:200]}")

    print("duckdb", duckdb.__version__)
    con.execute(f"CREATE SECRET (TYPE iceberg, TOKEN '{TOKEN}')")
    for auth in ("none", "oauth2"):  # `none` sends no token and must be refused (401)
        try:
            con.execute(
                f"ATTACH 'local' AS ice (TYPE iceberg, ENDPOINT 'http://127.0.0.1:{port}/iceberg', AUTHORIZATION_TYPE '{auth}')"
            )
            print(f"works   attach (AUTHORIZATION_TYPE {auth})")
            if auth == "oauth2":
                break
            con.execute("DETACH ice")
        except Exception as e:
            print(f"FAILS   attach (AUTHORIZATION_TYPE {auth}): {str(e).splitlines()[0][:200]}")
    attempt(
        "list tables",
        lambda: con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name='ice'").fetchall(),
    )
    attempt("select", lambda: con.execute("SELECT * FROM ice.shop.orders ORDER BY id").fetchall())
    attempt("insert", lambda: con.execute("INSERT INTO ice.shop.orders VALUES (4, 'd')").fetchall())
    attempt("pyiceberg sees the insert", lambda: cat.read_table("shop.orders")["id"].tolist())
    attempt("update", lambda: con.execute("UPDATE ice.shop.orders SET name='z' WHERE id=1").fetchall())
    attempt("delete", lambda: con.execute("DELETE FROM ice.shop.orders WHERE id=2").fetchall())
    attempt("pyiceberg sees update and delete", lambda: cat.read_table("shop.orders").sort_values("id").values.tolist())
    attempt("push blockers after delete", lambda: cat.push_blockers("shop.orders"))
    attempt("create schema", lambda: con.execute("CREATE SCHEMA ice.duck").fetchall())
    attempt("create table", lambda: con.execute("CREATE TABLE ice.duck.t (a INT, b VARCHAR)").fetchall())
    attempt("insert into new table", lambda: con.execute("INSERT INTO ice.duck.t VALUES (1, 'x')").fetchall())
    attempt("pyiceberg reads duckdb's table", lambda: cat.read_table("duck.t").values.tolist())
    attempt("create table as", lambda: con.execute("CREATE TABLE ice.duck.t2 AS SELECT 1 AS a").fetchall())
    attempt("drop table", lambda: con.execute("DROP TABLE ice.duck.t").fetchall())
    print("tables now:", [t["name"] for t in cat.list_tables()])


if __name__ == "__main__":
    main("--install" in sys.argv)
