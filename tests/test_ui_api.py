"""L2 backend: type overrides, previews, folders, samples, snapshot summaries, cookie auth and static serving."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from resink_catalog import Catalog
from resink_catalog import app as app_module
from resink_catalog.app import create_app

TOKEN = "t0k"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def api(home):
    return TestClient(create_app(TOKEN, home), base_url="http://127.0.0.1:8765", headers=H)


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    (d / "sales.csv").write_text('id,amount,day\n1,"1,234.50",2026-10-01\n2,"2,000.00",2026-10-02\n')
    return d


def source_id(api, folder):
    return api.post("/api/v1/roots", json={"path": str(folder)}).json()[0]["id"]


def test_roots_list_counts_by_status(api, folder):
    assert api.get("/api/v1/roots").json() == []
    api.post("/api/v1/roots", json={"path": str(folder)})
    [root] = api.get("/api/v1/roots").json()
    assert root["path"] == str(folder.resolve()) and root["counts"] == {"proposed": 1}
    api.post("/api/v1/sources/import", json={"root": str(folder)})
    assert api.get("/api/v1/roots").json()[0]["counts"] == {"imported": 1}


def test_preview_shows_inferred_types_and_rows(api, folder):
    sid = source_id(api, folder)
    p = api.get(f"/api/v1/sources/{sid}/preview").json()
    assert [(c["name"], c["inferred"], c["type"]) for c in p["columns"]] == [
        ("id", "long", "long"),
        ("amount", "double", "double"),  # '1,234.50' recognised as a number
        ("day", "date", "date"),
    ]
    assert p["rows"][0] == {"id": 1, "amount": 1234.5, "day": "2026-10-01"} and p["total_rows"] == 2
    assert p["error"] is None


def test_override_then_import_changes_the_table_schema(api, folder, home):
    sid = source_id(api, folder)
    r = api.post(f"/api/v1/sources/{sid}/types", json={"column_types": {"id": "string", "day": "string"}})
    assert r.json()["column_types"] == {"id": "string", "day": "string"}
    p = api.get(f"/api/v1/sources/{sid}/preview").json()
    assert [(c["name"], c["inferred"], c["type"]) for c in p["columns"]][0] == ("id", "long", "string")
    assert p["rows"][0]["id"] == "1"
    done = api.post("/api/v1/sources/import", json={"source_id": sid}).json()
    assert done[0]["outcome"] == "created"
    schema = {c["name"]: c["type"] for c in api.get("/api/v1/tables/exports.sales").json()["schema"]}
    assert schema == {"id": "string", "amount": "double", "day": "string"}
    assert Catalog(home).read_table("exports.sales")["id"].tolist() == ["1", "2"]


def test_override_sent_with_the_import(api, folder):
    sid = source_id(api, folder)
    done = api.post("/api/v1/sources/import", json={"source_id": sid, "column_types": {"id": "double"}}).json()
    assert done[0]["outcome"] == "created"
    assert {c["name"]: c["type"] for c in api.get("/api/v1/tables/exports.sales").json()["schema"]}["id"] == "double"
    assert api.get("/api/v1/sources").json()[0]["column_types"] == {"id": "double"}  # remembered


def test_override_that_does_not_fit_fails_loudly_and_imports_nothing(api, folder):
    sid = source_id(api, folder)
    done = api.post("/api/v1/sources/import", json={"source_id": sid, "column_types": {"amount": "date"}}).json()
    assert done[0]["outcome"] == "error" and "convert" in done[0]["error"]
    assert api.get("/api/v1/tables").json() == []
    preview = api.get(f"/api/v1/sources/{sid}/preview").json()
    assert preview["error"] and preview["rows"]  # the data is still shown, as inferred


@pytest.mark.parametrize("types", [{"nope": "string"}, {"id": "varchar"}], ids=["unknown column", "unknown type"])
def test_bad_overrides_are_refused(api, folder, types):
    sid = source_id(api, folder)
    r = api.post(f"/api/v1/sources/{sid}/types", json={"column_types": types})
    if r.status_code == 200:  # an unknown column is only detectable when reading the file
        done = api.post("/api/v1/sources/import", json={"source_id": sid}).json()
        assert done[0]["outcome"] == "error" and "no such column" in done[0]["error"]
    else:
        assert r.status_code == 400 and "unknown type" in r.json()["detail"]


def test_unknown_source_is_a_400(api):
    assert api.get("/api/v1/sources/999/preview").status_code == 400
    assert api.post("/api/v1/sources/999/types", json={"column_types": {}}).status_code == 400


def test_types_cannot_be_sent_with_a_folder_import(api, folder):
    api.post("/api/v1/roots", json={"path": str(folder)})
    r = api.post("/api/v1/sources/import", json={"root": str(folder), "column_types": {"id": "string"}})
    assert r.status_code == 400


def test_table_sample_and_snapshot_history(api, home):
    cat = Catalog(home)
    cat.write_table("shop.orders", pd.DataFrame({"id": list(range(30))}))
    cat.write_table("shop.orders", pd.DataFrame({"id": [100]}), mode="append")
    s = api.get("/api/v1/tables/shop.orders/sample", params={"limit": 5}).json()
    assert s["columns"] == ["id"] and len(s["rows"]) == 5
    snaps = api.get("/api/v1/tables/shop.orders").json()["snapshots"]
    assert [(x["operation"], x["added_records"], x["total_records"]) for x in snaps] == [
        ("append", 30, 30),
        ("append", 1, 31),
    ]
    assert api.get("/api/v1/tables/nope.nope/sample").status_code == 404


def test_old_catalog_without_column_types_is_upgraded(tmp_path):
    import sqlite3

    home = tmp_path / "h"
    home.mkdir()
    conn = sqlite3.connect(home / "catalog.db")
    conn.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "CREATE TABLE data_roots (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE,"
        " name TEXT NOT NULL, last_scan TEXT);"
        "CREATE TABLE data_sources (id INTEGER PRIMARY KEY AUTOINCREMENT, root_id INTEGER, path TEXT NOT NULL UNIQUE,"
        " format TEXT NOT NULL, content_hash TEXT, target_table TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'overwrite',"
        " last_import TEXT, status TEXT NOT NULL, error TEXT, rows INTEGER);"
        "INSERT INTO data_sources (path, format, target_table, status) VALUES ('/x.csv', 'csv', 'main.x', 'proposed');"
    )
    conn.commit()
    conn.close()
    assert Catalog(home).sources()[0]["column_types"] == {}


# ------------------------------------------------------------------ browser access: cookie and static files


@pytest.fixture
def static_dir(tmp_path, monkeypatch):
    d = tmp_path / "static"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>UI</title><div id=root></div>")
    (d / "assets" / "app.js").write_text("console.log(1)")
    (d / "favicon.svg").write_text("<svg/>")
    monkeypatch.setattr(app_module, "STATIC_DIR", d)
    return d


def test_the_page_files_are_public_but_the_api_is_not(home, static_dir):
    c = TestClient(create_app(TOKEN, home), base_url="http://127.0.0.1:8765")
    assert "<div id=root>" in c.get("/").text and c.get("/assets/app.js").text == "console.log(1)"
    assert c.get("/favicon.svg").status_code == 200
    assert "<title>UI</title>" in c.get("/tables/shop.orders").text  # client-side route falls back to the app
    assert c.get("/api/v1/tables").status_code == 401
    assert c.get("/api/v1/tables", headers=H).status_code == 200


def test_no_response_ever_sets_a_cookie(home, static_dir):
    """A cookie would be sent to every service on 127.0.0.1 whatever its port (cookies ignore ports)."""
    c = TestClient(create_app(TOKEN, home), base_url="http://127.0.0.1:8765")
    for path, headers in [
        ("/", {}),
        (f"/?token={TOKEN}", {}),
        ("/api/v1/tables", H),
        (f"/api/v1/tables?token={TOKEN}", {}),
        ("/api/v1/tables", {"Authorization": "Bearer nope"}),
        ("/healthz", {}),
    ]:
        r = c.get(path, headers=headers)
        assert "set-cookie" not in r.headers, path
    assert not c.cookies  # nothing to replay


def test_a_cookie_is_not_a_credential(home, static_dir):
    c = TestClient(create_app(TOKEN, home), base_url="http://127.0.0.1:8765", cookies={"rc_token": TOKEN})
    assert c.get("/api/v1/tables").status_code == 401


def test_browser_writes_from_another_origin_are_refused_even_with_the_token(home, static_dir):
    c = TestClient(create_app(TOKEN, home), base_url="http://127.0.0.1:8765", headers=H)
    body = {"sql": "select 1"}
    assert c.post("/api/v1/query", json=body, headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200
    assert c.post("/api/v1/query", json=body).status_code == 200  # CLI clients send no Origin
    for evil in ("http://127.0.0.1:9999", "http://evil.example", "http://localhost:8765"):
        assert c.post("/api/v1/query", json=body, headers={"Origin": evil}).status_code == 403, evil


def test_path_traversal_is_not_served(home, static_dir, tmp_path):
    (tmp_path / "secret.txt").write_text("secret")
    c = TestClient(create_app(TOKEN, home), base_url="http://127.0.0.1:8765", headers=H)
    assert "secret" not in c.get("/../secret.txt").text
    assert "secret" not in c.get("/%2e%2e/secret.txt").text


def test_api_paths_that_match_nothing_stay_404_not_the_app(home, static_dir):
    c = TestClient(create_app(TOKEN, home), base_url="http://127.0.0.1:8765", headers=H)
    assert c.get("/api/v1/nothing").status_code == 404
    assert c.get("/iceberg/v1/nothing").status_code == 404


def test_without_a_built_ui_the_root_explains_it(home, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "STATIC_DIR", tmp_path / "missing")
    c = TestClient(create_app(TOKEN, home), base_url="http://127.0.0.1:8765", headers=H)
    r = c.get("/")
    assert r.status_code == 200 and "scripts/build_ui.sh" in r.text
