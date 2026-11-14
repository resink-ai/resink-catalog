"""The PyIceberg SQL catalog on catalog.db, with data under <home>/warehouse/."""

from __future__ import annotations

import re
from pathlib import Path

from pyiceberg.catalog import Catalog as IcebergCatalog
from pyiceberg.catalog.sql import SqlCatalog

DEFAULT_NAMESPACE = "default"


def open_iceberg(home: Path) -> IcebergCatalog:
    warehouse = home / "warehouse"
    warehouse.mkdir(parents=True, exist_ok=True)
    return SqlCatalog(
        "local",
        uri=f"sqlite:///{home / 'catalog.db'}",
        warehouse=warehouse.as_uri(),
    )


def split_name(name: str) -> tuple[str, str]:
    """'sales.monthly' -> ('sales', 'monthly'); 'monthly' -> ('default', 'monthly')."""
    parts = name.split(".")
    if len(parts) == 1:
        return DEFAULT_NAMESPACE, parts[0]
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    raise ValueError(f"table name must be 'table' or 'namespace.table', got {name!r}")


def slug(text: str, fallback: str = "table") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if not s:
        return fallback
    return s if not s[0].isdigit() else f"t_{s}"
