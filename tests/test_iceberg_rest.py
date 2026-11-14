"""PyIceberg's REST client against our /iceberg/v1 endpoint: list, read, write, evolve, drop."""

import pandas as pd
import pyarrow as pa
import pytest
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchNamespaceError, NoSuchTableError
from pyiceberg.types import StringType


@pytest.fixture
def rest(live_server) -> RestCatalog:
    return RestCatalog("remote", uri=live_server.iceberg_uri, token=live_server.token)


def test_rest_requires_token(live_server):
    import httpx

    assert httpx.get(f"{live_server.iceberg_uri}/v1/config").status_code == 401
    ok = httpx.get(f"{live_server.iceberg_uri}/v1/config", headers={"Authorization": f"Bearer {live_server.token}"})
    assert ok.status_code == 200


def test_namespace_lifecycle(rest):
    rest.create_namespace("shop", {"owner": "me"})
    assert ("shop",) in rest.list_namespaces()
    assert rest.load_namespace_properties("shop")["owner"] == "me"
    with pytest.raises(NamespaceAlreadyExistsError):
        rest.create_namespace("shop")
    rest.drop_namespace("shop")
    with pytest.raises(NoSuchNamespaceError):
        rest.load_namespace_properties("shop")


def test_create_write_read_and_list(rest):
    rest.create_namespace("shop")
    data = pa.table({"id": [1, 2, 3], "name": ["a", "b", "c"]})
    t = rest.create_table("shop.orders", schema=data.schema)
    t.append(data)
    assert ("shop", "orders") in rest.list_tables("shop")
    loaded = rest.load_table("shop.orders")
    assert loaded.scan().to_arrow().sort_by("id").column("id").to_pylist() == [1, 2, 3]
    loaded.append(pa.table({"id": [4], "name": ["d"]}))
    assert rest.load_table("shop.orders").scan().to_arrow().num_rows == 4
    assert len(rest.load_table("shop.orders").metadata.snapshots) == 2


def test_schema_evolution_and_overwrite(rest):
    rest.create_namespace("shop")
    t = rest.create_table("shop.items", schema=pa.table({"id": [1]}).schema)
    t.append(pa.table({"id": [1]}))
    with t.update_schema() as u:
        u.add_column("label", StringType())
    t = rest.load_table("shop.items")
    t.overwrite(pa.table({"id": pa.array([9], pa.int64()), "label": ["x"]}))
    assert rest.load_table("shop.items").scan().to_arrow().to_pylist() == [{"id": 9, "label": "x"}]


def test_drop_and_missing_table(rest):
    rest.create_namespace("shop")
    rest.create_table("shop.tmp", schema=pa.table({"id": [1]}).schema)
    rest.drop_table("shop.tmp")
    with pytest.raises(NoSuchTableError):
        rest.load_table("shop.tmp")


def test_rest_and_local_api_see_the_same_tables(rest, live_server):
    """What a REST client writes is visible to the library and the local API, and the other way round."""
    from resink_catalog import Catalog

    rest.create_namespace("shop")
    rest.create_table("shop.rest_made", schema=pa.table({"v": [1]}).schema).append(pa.table({"v": [7]}))
    local = Catalog(live_server.home)
    assert local.read_table("shop.rest_made")["v"].tolist() == [7]
    local.write_table("shop.local_made", pd.DataFrame({"v": [1, 2]}))
    assert rest.load_table("shop.local_made").scan().to_arrow().num_rows == 2
