"""POST /iceberg/v1/transactions/commit: several tables, all or nothing."""

import httpx
import pandas as pd
import pytest

from resink_catalog import Catalog


@pytest.fixture
def two_tables(live_server):
    cat = Catalog(live_server.home)
    cat.write_table("shop.a", pd.DataFrame({"x": [1]}))
    cat.write_table("shop.b", pd.DataFrame({"x": [2]}))
    return cat


def change(cat, name, prop, uuid=None):
    ns, tbl = name.split(".")
    t = cat.iceberg.load_table((ns, tbl))
    return {
        "identifier": {"namespace": [ns], "name": tbl},
        "requirements": [{"type": "assert-table-uuid", "uuid": uuid or str(t.metadata.table_uuid)}],
        "updates": [{"action": "set-properties", "updates": {prop: "1"}}],
    }


def post(live_server, changes):
    return httpx.post(
        f"{live_server.iceberg_uri}/v1/transactions/commit",
        json={"table-changes": changes},
        headers={"Authorization": f"Bearer {live_server.token}"},
    )


def props(cat, name):
    ns, tbl = name.split(".")
    return cat.iceberg.load_table((ns, tbl)).properties


def test_both_tables_change(live_server, two_tables):
    r = post(live_server, [change(two_tables, "shop.a", "p"), change(two_tables, "shop.b", "p")])
    assert r.status_code == 204
    assert props(two_tables, "shop.a").get("p") == "1" and props(two_tables, "shop.b").get("p") == "1"


def test_a_failed_requirement_in_the_second_table_changes_neither(live_server, two_tables):
    bad = change(two_tables, "shop.b", "p", uuid="00000000-0000-0000-0000-000000000000")
    r = post(live_server, [change(two_tables, "shop.a", "p"), bad])
    assert r.status_code == 409 and r.json()["error"]["type"] == "CommitFailedException"
    assert "p" not in props(two_tables, "shop.a") and "p" not in props(two_tables, "shop.b")


def test_pointer_conflict_rolls_back_every_table(live_server, two_tables, monkeypatch):
    """A table changed by someone else between staging and the pointer swap: nothing may move."""

    from pyiceberg.catalog.sql import SqlCatalog

    orig = SqlCatalog._write_metadata
    raced = []

    def write_then_race(metadata, io, metadata_path):
        orig(metadata, io, metadata_path)
        if not raced:
            raced.append(True)
            with two_tables.iceberg.load_table(("shop", "b")).transaction() as tx:  # a concurrent writer
                tx.set_properties({"raced": "1"})

    monkeypatch.setattr(SqlCatalog, "_write_metadata", staticmethod(write_then_race))
    r = post(live_server, [change(two_tables, "shop.a", "p"), change(two_tables, "shop.b", "p")])
    monkeypatch.undo()
    assert r.status_code == 409
    assert "p" not in props(two_tables, "shop.a"), "table a moved although b conflicted"
    assert props(two_tables, "shop.b").get("raced") == "1" and "p" not in props(two_tables, "shop.b")


def test_unknown_table_is_404_and_duplicates_are_400(live_server, two_tables):
    ghost = change(two_tables, "shop.a", "p") | {"identifier": {"namespace": ["shop"], "name": "ghost"}}
    assert post(live_server, [change(two_tables, "shop.a", "p"), ghost]).status_code == 404
    assert "p" not in props(two_tables, "shop.a")
    dup = change(two_tables, "shop.a", "p")
    assert post(live_server, [dup, dup]).status_code == 400


def test_needs_the_token(live_server):
    assert (
        httpx.post(f"{live_server.iceberg_uri}/v1/transactions/commit", json={"table-changes": []}).status_code == 401
    )
