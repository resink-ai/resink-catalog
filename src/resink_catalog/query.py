"""Read-only SQL over the catalog's tables, run by DuckDB over Arrow data read through PyIceberg.

DuckDB's own `iceberg` extension is not used here: it has to be downloaded at first use, and the local catalog
must work offline (D10). The tables a query mentions are loaded into a throw-away in-memory DuckDB; file access
from SQL is switched off, so a query can only see catalog tables.
"""

from __future__ import annotations

import re
from typing import Any

import duckdb
import pyarrow as pa
from fastapi.encoders import jsonable_encoder
from pyiceberg.catalog import Catalog as IcebergCatalog

DEFAULT_LIMIT = 1000


class QueryError(Exception):
    """The query was refused or failed; the message is shown to the user."""


def check_read_only(con: duckdb.DuckDBPyConnection, sql: str) -> None:
    try:
        statements = con.extract_statements(sql)
    except duckdb.Error as e:
        raise QueryError(f"cannot parse SQL: {str(e).splitlines()[0]}") from e
    if len(statements) != 1:
        raise QueryError("exactly one statement is allowed")
    if statements[0].type != duckdb.StatementType.SELECT:
        raise QueryError("only read-only SELECT statements are allowed")


def _mentioned(catalog: IcebergCatalog, sql: str) -> list[tuple[str, str]]:
    lowered = sql.lower()
    out = []
    for ns in catalog.list_namespaces():
        for ident in catalog.list_tables(ns):
            if re.search(rf"\b{re.escape(ident[-1].lower())}\b", lowered):
                out.append(ident)
    return out


def query_arrow(catalog: IcebergCatalog, sql: str, limit: int | None = None) -> pa.Table:
    """Run a read-only query; `limit` (if given) is pushed into the SQL."""
    # No file, network or extension access, whatever the SQL says: external access off, extensions neither
    # auto-installed nor auto-loaded, and the settings locked so a statement cannot switch them back on.
    con = duckdb.connect(
        config={
            "enable_external_access": False,
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
        }
    )
    con.execute("SET lock_configuration = true")
    try:
        check_read_only(con, sql)
        for i, ident in enumerate(_mentioned(catalog, sql)):
            ns, name = ident[0], ident[-1]
            con.register(f"_src{i}", catalog.load_table(ident).scan().to_arrow())
            con.execute(f'CREATE SCHEMA IF NOT EXISTS "{ns}"')
            con.execute(f'CREATE VIEW "{ns}"."{name}" AS SELECT * FROM _src{i}')
        text = sql.strip().rstrip(";")
        if limit is not None:
            text = f"SELECT * FROM ({text}) AS _q LIMIT {int(limit)}"
        try:
            return con.sql(text).to_arrow_table()
        except duckdb.Error as e:
            raise QueryError(str(e).splitlines()[0]) from e
    finally:
        con.close()


def run_query(catalog: IcebergCatalog, sql: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """JSON-friendly result for the HTTP API: at most `limit` rows, `truncated` says if there were more."""
    table = query_arrow(catalog, sql, limit + 1)
    rows = table.to_pylist()
    return {
        "columns": table.column_names,
        "rows": jsonable_encoder([list(r.values()) for r in rows[:limit]]),
        "truncated": len(rows) > limit,
    }
