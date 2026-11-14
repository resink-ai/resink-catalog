"""L1 acceptance 4: a DataFrame written through the Python API is found through /api/v1/tables and /api/v1/query."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from resink_catalog import Catalog
from resink_catalog.app import create_app

TOKEN = "t0k"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def api(home):
    return TestClient(create_app(TOKEN, home), base_url="http://127.0.0.1:8765", headers=H)


def test_python_api_write_is_found_through_the_http_api(api, home):
    Catalog(home).write_table("shop.orders", pd.DataFrame({"id": [1, 2, 3], "qty": [5, 6, 7]}))
    tables = api.get("/api/v1/tables").json()
    assert [(t["name"], t["rows"]) for t in tables] == [("shop.orders", 3)]
    q = api.post("/api/v1/query", json={"sql": "select sum(qty) as total from shop.orders"}).json()
    assert q == {"columns": ["total"], "rows": [[18]], "truncated": False}
    info = api.get("/api/v1/tables/shop.orders").json()
    assert info["schema"][0]["name"] == "id"
    assert info["push_ready"] is True and info["push_blockers"] == []


def test_python_api_roundtrip(catalog):
    catalog.write_table("people", pd.DataFrame({"name": ["a", "b"]}))
    assert catalog.read_table("people")["name"].tolist() == ["a", "b"]
    assert catalog.query("select count(*) as n from people")["n"].tolist() == [2]  # default namespace
    catalog.write_table("people", pd.DataFrame({"name": ["c"]}), mode="append")
    assert len(catalog.read_table("people")) == 3
    catalog.write_table("people", pd.DataFrame({"name": ["z"]}))  # default mode replaces
    assert catalog.read_table("people")["name"].tolist() == ["z"]
    with pytest.raises(ValueError):
        catalog.write_table("people", pd.DataFrame({"name": ["z"]}), mode="merge")


@pytest.mark.parametrize(
    "sql",
    [
        "drop table shop.orders",
        "insert into shop.orders values (9, 9)",
        "update shop.orders set qty = 0",
        "delete from shop.orders",
        "create table x as select 1",
        "copy shop.orders to '/tmp/x.csv'",
        "attach '/tmp/evil.db'",
        "pragma enable_external_access",
        "select 1; drop table shop.orders",
        "select * from read_csv('/etc/passwd')",
        "install httpfs",
    ],
)
def test_query_refuses_anything_but_read_only_select(api, home, sql):
    Catalog(home).write_table("shop.orders", pd.DataFrame({"id": [1], "qty": [1]}))
    r = api.post("/api/v1/query", json={"sql": sql})
    assert r.status_code == 400, r.text
    assert api.post("/api/v1/query", json={"sql": "select count(*) from shop.orders"}).json()["rows"] == [[1]]


def test_query_limit_and_truncation(api, home):
    Catalog(home).write_table("n", pd.DataFrame({"i": list(range(50))}))
    r = api.post("/api/v1/query", json={"sql": "select i from n order by i", "limit": 10}).json()
    assert len(r["rows"]) == 10 and r["truncated"] is True


def test_sql_error_is_a_400_with_a_message(api):
    r = api.post("/api/v1/query", json={"sql": "select * from nope"})
    assert r.status_code == 400 and "nope" in r.json()["detail"]


def test_sources_and_import_over_http(api, tmp_path):
    folder = tmp_path / "exp"
    folder.mkdir()
    (folder / "a.csv").write_text("id,v\n1,x\n2,y\n")
    proposed = api.post("/api/v1/roots", json={"path": str(folder)}).json()
    assert [s["status"] for s in proposed] == ["proposed"]
    assert api.get("/api/v1/tables").json() == []  # proposing imports nothing
    done = api.post("/api/v1/sources/import", json={"root": str(folder)}).json()
    assert done[0]["outcome"] == "created"
    assert api.get("/api/v1/sources", params={"status": "imported"}).json()[0]["rows"] == 2
    assert api.post("/api/v1/sources/scan").json()["unchanged"]
    assert api.post("/api/v1/sources/import", json={}).status_code == 400
    assert api.post("/api/v1/sources/import", json={"root": str(tmp_path / "unknown")}).status_code == 400


def test_new_routes_need_the_token(home):
    bare = TestClient(create_app(TOKEN, home), base_url="http://127.0.0.1:8765")
    for method, path in [
        ("get", "/api/v1/tables"),
        ("post", "/api/v1/query"),
        ("get", "/api/v1/sources"),
        ("get", "/iceberg/v1/namespaces"),
        ("post", "/iceberg/v1/namespaces"),
    ]:
        assert getattr(bare, method)(path).status_code == 401, path


def test_the_default_namespace_keyword_gets_a_helpful_error(api, home):
    Catalog(home).write_table("t", pd.DataFrame({"a": [1]}))
    bad = api.post("/api/v1/query", json={"sql": "select count(*) from default.t"})
    assert bad.status_code == 400 and 'is an SQL keyword' in bad.json()["detail"]
    for ok in ('select count(*) from "default"."t"', "select count(*) from t", 'select count(*) from "default".t'):
        assert api.post("/api/v1/query", json={"sql": ok}).json()["rows"] == [[1]], ok
