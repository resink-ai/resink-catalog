import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from fake_cloud import ACCOUNTS, PUSH, FakeCloud, browser
from push_helpers import Sleeps

from resink_catalog.app import create_app
from resink_catalog.catalog import Catalog
from resink_catalog.cloud.signin import login
from resink_catalog.credentials import FileStore

TOKEN = "test-token-value"


@dataclass
class LiveServer:
    url: str
    token: str
    home: Path

    @property
    def iceberg_uri(self) -> str:
        return f"{self.url}/iceberg"


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def catalog(home) -> Catalog:
    return Catalog(home)


@pytest.fixture
def live_server(home):
    """The real app on a free 127.0.0.1 port, for clients that speak HTTP (PyIceberg, DuckDB)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(TOKEN, home), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "server did not start"
    yield LiveServer(f"http://127.0.0.1:{port}", TOKEN, home)
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------- push client (L5a): a fake cloud and a signed-in device


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud()


@pytest.fixture
def cred_dir(tmp_path, monkeypatch):
    d = tmp_path / "cred"
    monkeypatch.setenv("RESINK_CATALOG_CREDENTIALS_DIR", str(d))
    return d


@pytest.fixture
def store(cred_dir) -> FileStore:
    return FileStore(cred_dir)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def device(cloud, store, sleeps):
    return login(
        ACCOUNTS,
        PUSH,
        store=store,
        transport=cloud.transport(),
        open_browser=browser(cloud),
        out=lambda s: None,
        sleep=sleeps.append,
        clock=sleeps.clock,
    )
