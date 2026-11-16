"""Device sign-in (push protocol section 3): authorization code with PKCE (S256) and a loopback redirect, refresh
with rotation, and logout.

Where things are kept:
- the access and refresh tokens: the OS credential store (`credentials.open_store`), never `catalog.db`;
- what is not secret (device id, org, the workspaces offered at sign-in, the chosen target workspace, the cloud URLs
  and the access token's expiry): `device.json` next to the credentials file, written with mode 0600 as well.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import os
import secrets
import socket
import tempfile
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2

from ..credentials import NoSecureStore, TokenStore, credentials_dir, open_store
from .http import Api, CloudError, fail

CLIENT_ID = "resink-catalog"
# Placeholders from the contract's `servers` (hosts not decided). Tests always pass their own URLs.
DEFAULT_ACCOUNTS_URL = "https://accounts.resink.ai"
DEFAULT_PUSH_URL = "https://api.resink.ai"
ACCOUNTS_URL_ENV = "RESINK_ACCOUNTS_URL"
PUSH_URL_ENV = "RESINK_PUSH_URL"
LOGIN_TIMEOUT_SECONDS = 300  # section 3: the client gives up after 5 minutes
EXPIRY_MARGIN_SECONDS = 60  # refresh an access token this long before it expires
ACCESS, REFRESH = "access_token", "refresh_token"


class LoginError(CloudError):
    pass


def device_file() -> Path:
    return credentials_dir() / "device.json"


def cloud_urls(
    accounts_url: str | None = None, push_url: str | None = None, saved: dict | None = None
) -> tuple[str, str]:
    """A flag wins, then the environment, then what was used at sign-in, then the default."""
    saved = saved or {}
    a = accounts_url or os.environ.get(ACCOUNTS_URL_ENV) or saved.get("accounts_url") or DEFAULT_ACCOUNTS_URL
    p = push_url or os.environ.get(PUSH_URL_ENV) or saved.get("push_url") or DEFAULT_PUSH_URL
    return a.rstrip("/"), p.rstrip("/")


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.parent.chmod(0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".device-")  # created 0600
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def pkce() -> tuple[str, str]:
    """RFC 7636: a 64-character verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


# ------------------------------------------------------------------------------------------- loopback listener


class _CallbackServer(http.server.HTTPServer):
    result: dict | None = None


class _Handler(http.server.BaseHTTPRequestHandler):
    timeout = 10  # a connection that sends nothing cannot hold the listener

    def do_GET(self):  # noqa: N802
        url = urlsplit(self.path)
        if url.path != "/callback" or self.server.result is not None:
            self.send_error(404)
            return
        self.server.result = {k: v[0] for k, v in parse_qs(url.query).items()}
        body = b"<!doctype html><title>resink-catalog</title><p>You can close this tab and return to the terminal.</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the terminal quiet
        pass


class CallbackListener:
    """Listens on 127.0.0.1 only, on a free port; accepts exactly one request to /callback, then closes."""

    def __init__(self):
        self.server = _CallbackServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.redirect_uri = f"http://127.0.0.1:{self.port}/callback"

    def wait(self, state: str, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        try:
            while self.server.result is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LoginError("sign-in timed out after 5 minutes; run `resink-catalog login` again")
                self.server.timeout = min(remaining, 1.0)
                self.server.handle_request()
        finally:
            self.close()
        q = self.server.result
        if not hmac.compare_digest(q.get("state", ""), state):
            raise LoginError("sign-in refused: the callback's state does not match this login (possible forgery)")
        if "error" in q:
            raise LoginError(f"sign-in was not completed: {q['error']} {q.get('error_description', '')}".strip())
        if not q.get("code"):
            raise LoginError("sign-in refused: the callback carries no code")
        return q["code"]

    def close(self) -> None:
        self.server.server_close()


# ------------------------------------------------------------------------------------------- the signed-in device


@dataclass
class Device:
    """The signed-in device: its tokens (in `store`) and its non-secret record (`info`, saved in device.json)."""

    store: TokenStore
    info: dict
    transport: httpx2.BaseTransport | None = None
    sleep: Callable[[float], None] = field(default=time.sleep)
    path: Path = field(default_factory=device_file)

    @classmethod
    def load(cls, store: TokenStore | None = None, **kw) -> Device:
        path = kw.pop("path", None) or device_file()
        try:
            info = json.loads(path.read_text())
        except FileNotFoundError:
            raise CloudError("not signed in: run `resink-catalog login` first") from None
        store = store or _open_store()
        if not store.get(REFRESH):
            raise CloudError("not signed in: run `resink-catalog login` first")
        return cls(store=store, info=info, path=path, **kw)

    @property
    def accounts_url(self) -> str:
        return cloud_urls(saved=self.info)[0]

    @property
    def push_url(self) -> str:
        return cloud_urls(saved=self.info)[1]

    def default_workspace(self) -> str:
        ws = self.info.get("target_workspace_id")
        if not ws:
            raise CloudError("no target workspace chosen at sign-in: pass --workspace <id>")
        return ws

    def save(self, tokens: dict) -> None:
        """Store a token response: the refresh token first, so a crash can never lose the only valid one."""
        self.store.set(REFRESH, tokens["refresh_token"])
        self.store.set(ACCESS, tokens["access_token"])
        self.info["access_expires_at"] = time.time() + int(tokens["expires_in"])
        _write_private(self.path, self.info)

    def access_token(self) -> str:
        token = self.store.get(ACCESS)
        if not token or time.time() >= self.info.get("access_expires_at", 0) - EXPIRY_MARGIN_SECONDS:
            self.refresh()
            token = self.store.get(ACCESS)
        return token

    def refresh(self) -> None:
        """Rotate the refresh token. If accounts refuses it (reused, revoked, expired) the local tokens are useless:
        delete them and tell the user to sign in again."""
        api = Api(self.accounts_url, transport=self.transport, sleep=self.sleep)
        try:
            body = {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": self.store.get(REFRESH) or "",
            }
            r = api.request("POST", "/v1/device/token", json=body)
        finally:
            api.close()
        if r.status_code != 200:
            err = fail(r)
            if r.status_code in (400, 401):
                self.forget()
                reason = err.problem.get("detail") or err.problem.get("title")
                problem = {
                    "title": "Your sign-in is no longer valid",
                    "code": err.code,
                    "detail": f"accounts refused the refresh token ({reason}). Run `resink-catalog login` again.",
                }
                raise CloudError(problem["detail"], problem, r.status_code)
            raise err
        self.save(r.json())

    def forget(self) -> None:
        self.store.delete(ACCESS)
        self.store.delete(REFRESH)
        self.path.unlink(missing_ok=True)


def _open_store() -> TokenStore:
    try:
        return open_store()
    except NoSecureStore as e:
        raise CloudError(str(e)) from None


def login(
    accounts_url: str | None = None,
    push_url: str | None = None,
    *,
    workspace: str | None = None,
    store: TokenStore | None = None,
    transport: httpx2.BaseTransport | None = None,
    open_browser: Callable[[str], object] | None = webbrowser.open,
    out: Callable[[str], None] = print,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
    device_name: str | None = None,
    path: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Device:
    accounts_url, push_url = cloud_urls(accounts_url, push_url)
    store = store or _open_store()  # before the browser: fail now, not after the user signed in
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(32)
    listener = CallbackListener()
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": listener.redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "device_name": (device_name or socket.gethostname() or "resink-catalog")[:80],
    }
    url = f"{accounts_url}/v1/device/authorize?{urlencode(params)}"
    out(f"Sign in in your browser. If it does not open, visit:\n  {url}")
    try:
        if open_browser is not None:
            open_browser(url)
        code = listener.wait(state, timeout)
    finally:
        listener.close()
    api = Api(accounts_url, transport=transport, sleep=sleep)
    try:
        r = api.request(
            "POST",
            "/v1/device/token",
            json={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": listener.redirect_uri,
            },
        )
    finally:
        api.close()
    if r.status_code != 200:
        raise LoginError(**_err(fail(r, "sign-in failed: ")))
    tokens = r.json()
    workspaces = tokens.get("workspaces", [])
    chosen = workspace or next((w["id"] for w in workspaces if w.get("default")), None)
    if chosen is None and len(workspaces) == 1:
        chosen = workspaces[0]["id"]
    info = {
        "device_id": tokens["device_id"],
        "org_id": tokens["org_id"],
        "workspaces": workspaces,
        "target_workspace_id": chosen,
        "accounts_url": accounts_url,
        "push_url": push_url,
    }
    device = Device(store=store, info=info, transport=transport, sleep=sleep, path=path or device_file())
    device.save(tokens)
    return device


def _err(e: CloudError) -> dict:
    return {"message": str(e), "problem": e.problem, "status": e.status}


def logout(device: Device, *, local_only: bool = False) -> None:
    """Revoke the device at accounts (with its own device token), then delete the local tokens. If accounts cannot be
    reached the tokens are kept, so the user can retry; `local_only` deletes them without revoking."""
    if not local_only:
        try:
            token = device.access_token()
        except CloudError as e:
            if e.status in (400, 401):  # the refresh token is already refused: the device is revoked or expired
                device.forget()
                return
            raise
        api = Api(device.accounts_url, transport=device.transport, sleep=device.sleep)
        try:
            r = api.request("POST", "/v1/device/revoke", headers={"Authorization": f"Bearer {token}"})
        finally:
            api.close()
        if r.status_code != 204:
            raise fail(r, "logout failed (your local tokens are kept; retry, or use --local): ")
    device.forget()
