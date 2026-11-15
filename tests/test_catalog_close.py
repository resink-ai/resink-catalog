"""close() disposes the pooled SQLite connections before it releases the lock, and a closed catalog refuses work."""

import pandas as pd
import pytest

from resink_catalog import Catalog
from resink_catalog.catalog import ImportErrorForUser
from resink_catalog.lock import CatalogInUse, CatalogLock


def test_close_disposes_the_engine_then_releases_the_lock_and_refuses_further_work(home):
    cat = Catalog(home)
    cat.write_table("t", pd.DataFrame({"a": [1]}))
    engine = cat.iceberg.engine
    assert engine.pool.checkedin() >= 1  # PyIceberg's SQLite connections are pooled
    with pytest.raises(CatalogInUse):
        CatalogLock(home, exclusive=True)  # open: no exclusive lock
    cat.close()
    assert engine.pool.checkedin() == 0  # the old pool is gone: no handle on catalog.db remains
    CatalogLock(home, exclusive=True).release()  # and the lock is free
    for call in (
        cat.list_tables,
        cat.sources,
        lambda: cat.query("select 1"),
        lambda: cat.iceberg,
        lambda: cat.list_tags("t"),
    ):
        with pytest.raises(ImportErrorForUser, match="closed"):
            call()
    cat.close()  # idempotent


def test_a_failed_initialisation_releases_the_lock_and_the_connections(home, monkeypatch):
    from resink_catalog import catalog as catalog_module

    def boom(path):
        raise RuntimeError("init failed")

    monkeypatch.setattr(catalog_module, "init_db", boom)
    with pytest.raises(RuntimeError, match="init failed"):
        Catalog(home)
    CatalogLock(home, exclusive=True).release()  # nothing was left holding the directory


def test_context_manager_closes(home):
    with Catalog(home) as cat:
        cat.write_table("t", pd.DataFrame({"a": [1]}))
    with pytest.raises(ImportErrorForUser):
        cat.list_tables()
    CatalogLock(home, exclusive=True).release()
