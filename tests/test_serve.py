"""Start the real CLI as a subprocess and check it from the outside."""

import re
import subprocess
import sys
import urllib.error
import urllib.request

import pytest


@pytest.fixture
def server(tmp_path):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "resink_catalog.cli",
            "serve",
            "--port",
            "0",
            "--no-browser",
            "--home",
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline()
    m = re.search(r"http://127\.0\.0\.1:(\d+)/\?token=(\S+)", line)
    assert m, line
    yield f"http://127.0.0.1:{m.group(1)}", m.group(2)
    proc.terminate()
    proc.wait(timeout=10)


def _status(url, token=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def test_serve_end_to_end(server, tmp_path):
    base, token = server
    assert _status(f"{base}/healthz") == 200
    assert _status(f"{base}/api/v1/info") == 401
    assert _status(f"{base}/api/v1/info", token) == 200
    assert (tmp_path / "catalog.db").exists()
    assert (tmp_path / "token").read_text().strip() == token
