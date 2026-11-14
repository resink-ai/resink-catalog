import pytest
from fastapi.testclient import TestClient

from resink_catalog.app import create_app

TOKEN = "test-token-value"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(TOKEN), base_url="http://127.0.0.1:8765")


def test_healthz_is_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/api/v1/info", "/", "/iceberg/v1/config", "/nope/at/all"])
def test_everything_else_requires_token(client, path):
    r = client.get(path)
    assert r.status_code == 401


def test_wrong_token_rejected(client):
    r = client.get("/api/v1/info", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_bearer_token_accepted(client):
    r = client.get("/api/v1/info", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["name"] == "resink-catalog"


def test_query_token_accepted(client):
    assert client.get(f"/api/v1/info?token={TOKEN}").status_code == 200


def test_unknown_api_route_with_token_is_404(client):
    r = client.get("/api/v1/nope", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 404


def test_non_loopback_host_header_rejected():
    c = TestClient(create_app(TOKEN), base_url="http://evil.example")
    r = c.get("/healthz")
    assert r.status_code == 403
