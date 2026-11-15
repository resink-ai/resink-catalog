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
    assert [(t["tag"], t["source"]) for t in info["tags"]] == [("customers", "ai")]
    assert [(n["body"], n["author"]) for n in info["notes"]] == [("Written by the assistant.", "ai")]


def test_the_user_can_remove_any_tag_or_note(api, home):
    cat = Catalog(home)
    cat.add_tags("shop.orders", ["wrong"], source="ai")
    note = cat.write_note("shop.orders", "wrong too", author="ai")
    tag_id = api.get("/api/v1/tables/shop.orders").json()["tags"][0]["id"]
    assert api.delete(f"/api/v1/tags/{tag_id}").status_code == 200
    assert api.delete(f"/api/v1/notes/{note['id']}").status_code == 200
    info = api.get("/api/v1/tables/shop.orders").json()
    assert info["tags"] == [] and info["notes"] == []
    assert api.delete(f"/api/v1/tags/{tag_id}").status_code == 404
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


def test_a_tag_with_a_slash_or_other_odd_characters_can_be_removed(api, home):
    Catalog(home).add_tags("shop.orders", ["sales/monthly", "50% off", "a b"], source="ai")
    for t in api.get("/api/v1/tables/shop.orders").json()["tags"]:
        assert api.delete(f"/api/v1/tags/{t['id']}").status_code == 200, t
    assert api.get("/api/v1/tables/shop.orders").json()["tags"] == []


def test_column_notes_of_a_look_alike_table_do_not_leak(home):
    """'_' is a LIKE wildcard: shop.sales_2026 must not pick up notes of shop.salesX2026."""
    cat = Catalog(home)
    for name in ("shop.sales_2026", "shop.salesX2026", "shop.sales%"):
        cat.write_table(name, pd.DataFrame({"v": [1]}))
    cat.write_note("shop.salesX2026.v", "belongs to X", author="user")
    cat.write_note("shop.sales_2026.v", "belongs to underscore", author="user")
    assert [n["body"] for n in cat.list_notes("shop.sales_2026")] == ["belongs to underscore"]
    assert [n["body"] for n in cat.table_info("shop.sales_2026")["notes"]] == ["belongs to underscore"]
    assert cat.count_notes("shop.sales_2026") == 1 and cat.list_notes("shop.sales%") == []


def test_ai_annotations_have_cumulative_limits_and_leave_the_user_room(home, monkeypatch):
    from resink_catalog import catalog as cat_module
    from resink_catalog.catalog import ImportErrorForUser

    monkeypatch.setitem(cat_module.LIMITS, "ai", {"tags_per_table": 3, "notes_per_table": 2, "notes_total": 3})
    cat = Catalog(home)
    cat.write_table("shop.orders", pd.DataFrame({"id": [1]}))
    cat.write_table("shop.items", pd.DataFrame({"id": [1]}))
    cat.add_tags("shop.orders", ["a", "b", "c"], source="ai")
    cat.add_tags("shop.orders", ["a"], source="ai")  # already there: not counted again
    with pytest.raises(ImportErrorForUser, match="limit is 3"):
        cat.add_tags("shop.orders", ["d"], source="ai")
    cat.add_tags("shop.orders", ["d"], source="user")  # the user's own room is separate
    cat.write_note("shop.orders", "1", author="ai")
    cat.write_note("shop.orders.id", "2", author="ai")
    with pytest.raises(ImportErrorForUser, match="too many notes"):
        cat.write_note("shop.orders", "3", author="ai")
    cat.write_note("shop.items", "3", author="ai")  # another table: still under the total of 3
    with pytest.raises(ImportErrorForUser, match="in all"):
        cat.write_note("shop.items", "4", author="ai")
    cat.write_note("shop.orders", "mine", author="user")  # the user is not locked out


def test_notes_are_read_a_page_at_a_time(api, home):
    cat = Catalog(home)
    for i in range(7):
        cat.write_note("shop.orders", f"n{i}", author="user")
    first = api.get("/api/v1/tables/shop.orders", params={"notes_limit": 3}).json()
    assert [n["body"] for n in first["notes"]] == ["n0", "n1", "n2"]
    assert first["notes_total"] == 7 and first["notes_next"] == first["notes"][-1]["id"]
    rest = api.get("/api/v1/tables/shop.orders", params={"notes_after": first["notes_next"], "notes_limit": 50}).json()
    assert [n["body"] for n in rest["notes"]] == ["n3", "n4", "n5", "n6"] and rest["notes_next"] is None
