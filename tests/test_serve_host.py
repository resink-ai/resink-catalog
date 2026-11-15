"""The catalog listens on 127.0.0.1 only; the one exception is inside the Docker image."""

from resink_catalog.cli import CONTAINER_ENV, _bind, listen_host


def test_listens_on_loopback_unless_in_the_container(monkeypatch):
    monkeypatch.delenv(CONTAINER_ENV, raising=False)
    assert listen_host() == "127.0.0.1"
    for value in ("0", "true", "yes", ""):
        monkeypatch.setenv(CONTAINER_ENV, value)
        assert listen_host() == "127.0.0.1", value
    monkeypatch.setenv(CONTAINER_ENV, "1")
    assert listen_host() == "0.0.0.0"


def test_the_socket_is_bound_to_loopback_by_default(monkeypatch):
    monkeypatch.delenv(CONTAINER_ENV, raising=False)
    sock = _bind(0)
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()
