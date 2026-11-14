"""Atomic multi-table commit for `POST /iceberg/v1/transactions/commit` (DuckDB commits through it).

Design section 4 left multi-table transactions out of scope; DuckDB's iceberg extension needs this endpoint for any
INSERT, UPDATE or DELETE, so L1 criterion 3 (DuckDB writes) brought it in.

How it stays all-or-nothing on SQLite:
1. every change is staged against the table's current metadata, which checks all of its requirements; nothing is
   written yet, so a failed requirement in any table aborts the whole request with no effect;
2. the new metadata files are written (a file nobody points to is harmless garbage if the next step fails);
3. every pointer move runs in ONE database transaction, each as compare-and-swap on the old metadata location. If any
   table was changed by someone else meanwhile, the transaction rolls back and no table moves.

This uses private methods of PyIceberg's `SqlCatalog` (`_update_and_stage_table`, `_write_metadata`), which can change
between PyIceberg releases; `tests/test_multi_commit.py` pins the behaviour.
"""

from __future__ import annotations

from pyiceberg.catalog import Catalog as IcebergCatalog
from pyiceberg.catalog.sql import IcebergTables, SqlCatalog
from pyiceberg.exceptions import CommitFailedException, NoSuchTableError
from pyiceberg.table import CommitTableRequest
from pyiceberg.table.update import AssertCreate
from sqlalchemy import update
from sqlalchemy.orm import Session


def commit_tables(catalog: IcebergCatalog, changes: list[CommitTableRequest]) -> None:
    if not isinstance(catalog, SqlCatalog):
        raise TypeError("atomic multi-table commit needs the SQL catalog")
    identifiers = [(*c.identifier.namespace.root, c.identifier.name) for c in changes]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("a table may appear only once in a transaction")

    staged = []
    for ident, change in zip(identifiers, changes, strict=True):
        current = catalog.load_table(ident)  # NoSuchTableError -> 404 for the whole request
        requirements = tuple(r for r in change.requirements if not isinstance(r, AssertCreate))
        new = catalog._update_and_stage_table(current, current.name(), requirements, change.updates)
        if new.metadata != current.metadata:
            staged.append((ident, current, new))

    for _, _, new in staged:
        catalog._write_metadata(metadata=new.metadata, io=new.io, metadata_path=new.metadata_location)

    with Session(catalog.engine) as session:
        for ident, current, new in staged:
            result = session.execute(
                update(IcebergTables)
                .where(
                    IcebergTables.catalog_name == catalog.name,
                    IcebergTables.table_namespace == catalog.namespace_to_string(ident[:-1]),
                    IcebergTables.table_name == ident[-1],
                    IcebergTables.metadata_location == current.metadata_location,
                )
                .values(metadata_location=new.metadata_location, previous_metadata_location=current.metadata_location)
            )
            if result.rowcount < 1:
                session.rollback()
                raise CommitFailedException(
                    f"table {'.'.join(ident)} was changed by another process; nothing committed"
                )
        session.commit()


__all__ = ["NoSuchTableError", "commit_tables"]
