"""Tags and notes over the local API: what the web UI reads and writes, with who wrote each."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from resink_catalog import Catalog
from resink_catalog.app import create_app

TOKEN = "t0k"


@pytest.fixture
def api(home):
    Catalog(home).write_table("shop.orders", pd.DataFrame({"id": [1], "amount": [2.0]}))
    return TestClient(
        create_app(TOKEN, home), base_url="http://127.0.0.1:8765", headers={"Authorization": f"Bearer {TOKEN}"}
    )


def test_user_tags_and_notes_are_marked_user(api):
    assert (
        api.post("/api/v1/tables/shop.orders/tags", json={"tags": ["Sales", "monthly"]}).json()[0]["source"] == "user"
    )
    n = api.post("/api/v1/tables/shop.orders/notes", json={"body": "One row per order."}).json()
    assert (n["target"], n["author"]) == ("shop.orders", "user")
    col = api.post("/api/v1/tables/shop.orders/notes", json={"target": "shop.orders.amount", "body": "EUR"}).json()
    assert col["target"] == "shop.orders.amount"
    info = api.get("/api/v1/tables/shop.orders").json()
    assert {t["tag"]: t["source"] for t in info["tags"]} == {"sales": "user", "monthly": "user"}
    assert [(x["target"], x["author"]) for x in info["notes"]] == [
        ("shop.orders", "user"),
        ("shop.orders.amount", "user"),
    ]
    assert api.get("/api/v1/tables").json()[0]["tags"] == info["tags"]


def test_what_the_ai_wrote_is_visible_and_marked_ai(api, home):
    cat = Catalog(home)
    cat.add_tags("shop.orders", ["customers"], source="ai")
    cat.write_note("shop.orders", "Written by the assistant.", author="ai")
    info = api.get("/api/v1/tables/shop.orders").json()
    assert info["tags"] == [{"tag": "customers", "source": "ai"}]
    assert [(n["body"], n["author"]) for n in info["notes"]] == [("Written by the assistant.", "ai")]


def test_the_user_can_remove_any_tag_or_note(api, home):
    cat = Catalog(home)
    cat.add_tags("shop.orders", ["wrong"], source="ai")
    note = cat.write_note("shop.orders", "wrong too", author="ai")
    assert api.delete("/api/v1/tables/shop.orders/tags/wrong").status_code == 200
    assert api.delete(f"/api/v1/notes/{note['id']}").status_code == 200
    info = api.get("/api/v1/tables/shop.orders").json()
    assert info["tags"] == [] and info["notes"] == []
    assert api.delete("/api/v1/tables/shop.orders/tags/wrong").status_code == 404
    assert api.delete(f"/api/v1/notes/{note['id']}").status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/tables/nope.nope/tags", {"tags": ["x"]}),
        ("post", "/api/v1/tables/shop.orders/tags", {"tags": []}),
        ("post", "/api/v1/tables/nope.nope/notes", {"body": "x"}),
        ("post", "/api/v1/tables/shop.orders/notes", {"body": ""}),
        ("post", "/api/v1/tables/shop.orders/notes", {"target": "shop.orders.zzz", "body": "x"}),
    ],
)
def test_bad_annotations_are_400(api, method, path, body):
    assert getattr(api, method)(path, json=body).status_code == 400


def test_annotation_routes_need_the_token(home):
    bare = TestClient(create_app(TOKEN, home), base_url="http://127.0.0.1:8765")
    assert bare.post("/api/v1/tables/x/tags", json={"tags": ["a"]}).status_code == 401
    assert bare.delete("/api/v1/notes/1").status_code == 401


def test_annotations_survive_in_an_old_catalog_upgrade(home):
    """The tables are created on open, so a catalog made by L2 gains them without a migration step."""
    import sqlite3

    Catalog(home)
    conn = sqlite3.connect(home / "catalog.db")
    conn.executescript("DROP TABLE tags; DROP TABLE notes;")
    conn.commit()
    conn.close()
    cat = Catalog(home)
    cat.write_table("t", pd.DataFrame({"a": [1]}))
    assert cat.add_tags("t", ["x"])[0]["tag"] == "x"
