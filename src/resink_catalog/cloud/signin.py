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
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2

from ..credentials import NoSecureStore, TokenStore, credentials_dir, open_store
from ..lock import blocking_lock
from .http import Api, CloudError, check_url, fail

CLIENT_ID = "resink-catalog"
# Placeholders from the contract's `servers` (hosts not decided). Tests always pass their own URLs.
DEFAULT_ACCOUNTS_URL = "https://accounts.resink.ai"
DEFAULT_PUSH_URL = "https://api.resink.ai"
ACCOUNTS_URL_ENV = "RESINK_ACCOUNTS_URL"
PUSH_URL_ENV = "RESINK_PUSH_URL"
LOGIN_TIMEOUT_SECONDS = 300  # section 3: the client gives up after 5 minutes
CONNECTION_SECONDS = 10  # one loopback connection gets at most this long, and never past the login deadline
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
    return check_url(a), check_url(p)


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
    deadline: float = 0.0  # monotonic end of the whole login

    def handle_error(self, request, client_address):  # a peer cut off by its deadline is not worth a traceback
        pass


class _Handler(http.server.BaseHTTPRequestHandler):
    """Each connection has an absolute deadline: CONNECTION_SECONDS, capped by what is left of the login. A watchdog
    shuts the socket down when it passes, so a peer that trickles bytes cannot hold the listener open."""

    def setup(self):
        super().setup()
        limit = max(0.0, min(CONNECTION_SECONDS, self.server.deadline - time.monotonic()))
        self.connection.settimeout(limit or 0.001)
        self._watchdog = threading.Timer(limit, self._cut)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _cut(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self._watchdog.cancel()
        try:
            super().finish()
        except OSError:
            pass

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
        self.server.deadline = time.monotonic() + timeout
        try:
            while self.server.result is None:
                remaining = self.server.deadline - time.monotonic()
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


NOT_SIGNED_IN = "not signed in: run `resink-catalog login` first"


@dataclass
class Device:
    """The signed-in device: its tokens (in `store`) and its non-secret record (`info`, saved in device.json).

    Several processes may share these credentials (two `push` at once). Every change to them (a refresh, a sign-in,
    a logout) runs under one cross-process lock and re-reads them inside it, so a refresh token is never rotated
    twice (accounts would take that as reuse and revoke the device), and nobody deletes tokens another process has
    just saved."""

    store: TokenStore
    info: dict
    transport: httpx2.BaseTransport | None = None
    sleep: Callable[[float], None] = field(default=time.sleep)
    clock: Callable[[], float] = field(default=time.monotonic)
    path: Path = field(default_factory=device_file)

    @classmethod
    def load(cls, store: TokenStore | None = None, **kw) -> Device:
        path = kw.pop("path", None) or device_file()
        try:
            info = json.loads(path.read_text())
        except FileNotFoundError:
            raise CloudError(NOT_SIGNED_IN) from None
        store = store or _open_store()
        if not store.get(REFRESH):
            raise CloudError(NOT_SIGNED_IN)
        device = cls(store=store, info=info, path=path, **kw)
        cloud_urls(saved=info)  # refuse a non-https address before any token is sent
        return device

    def lock(self) -> blocking_lock:
        return blocking_lock(self.path.parent / ".credentials.lock")

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

    def _save(self, tokens: dict) -> None:
        """Store a token response (the caller holds the lock): the refresh token first, so a crash can never lose
        the only valid one."""
        self.store.set(REFRESH, tokens["refresh_token"])
        self.store.set(ACCESS, tokens["access_token"])
        self.info["access_expires_at"] = time.time() + int(tokens["expires_in"])
        _write_private(self.path, self.info)

    def _reload(self) -> None:
        try:
            self.info = json.loads(self.path.read_text())
        except FileNotFoundError:
            raise CloudError(NOT_SIGNED_IN) from None

    def _expired(self) -> bool:
        return time.time() >= self.info.get("access_expires_at", 0) - EXPIRY_MARGIN_SECONDS

    def access_token(self) -> str:
        token = self.store.get(ACCESS)
        if not token or self._expired():
            self.refresh(token)
            token = self.store.get(ACCESS)
        return token

    def refresh(self, stale: str | None = None) -> None:
        """Get a new access token in place of `stale` (the one this process found expired or refused). Under the
        lock: if another process has meanwhile stored a different, unexpired one, use it; else rotate the refresh
        token once. The exchange is never resent once it may have reached accounts. If accounts refuses the refresh
        token (reused, revoked, expired), the local tokens are useless: delete them, but only if they are still
        the ones this process sent, and tell the user to sign in again."""
        with self.lock():
            self._reload()
            current = self.store.get(ACCESS)
            if current and current != stale and not self._expired():
                return
            sent = self.store.get(REFRESH)
            if not sent:
                raise CloudError(NOT_SIGNED_IN)
            with Api(self.accounts_url, transport=self.transport, sleep=self.sleep, clock=self.clock) as api:
                body = {"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": sent}
                r = api.request("POST", "/v1/device/token", json=body, once=True)
            if r.status_code == 200:
                self._save(r.json())
                return
            err = fail(r)
            if err.code != "invalid_grant":
                raise err
            if self.store.get(REFRESH) == sent:
                self._forget()
            reason = err.problem.get("detail") or err.problem.get("title")
            problem = {
                "title": "Your sign-in is no longer valid",
                "code": err.code,
                "detail": f"accounts refused the refresh token ({reason}). Run `resink-catalog login` again.",
            }
            raise CloudError(problem["detail"], problem, r.status_code)

    def _forget(self) -> None:
        self.store.delete(ACCESS)
        self.store.delete(REFRESH)
        self.path.unlink(missing_ok=True)

    def forget(self) -> None:
        with self.lock():
            self._forget()


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
    clock: Callable[[], float] = time.monotonic,
) -> Device:
    accounts_url, push_url = cloud_urls(accounts_url, push_url)  # https only: checked before the browser opens
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
    grant = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": listener.redirect_uri,
    }
    with Api(accounts_url, transport=transport, sleep=sleep, clock=clock) as api:
        r = api.request("POST", "/v1/device/token", json=grant, once=True)  # a code is single use: never resent
    if r.status_code != 200:
        err = fail(r, "sign-in failed: ")
        raise LoginError(str(err), err.problem, err.status)
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
    device = Device(store=store, info=info, transport=transport, sleep=sleep, clock=clock, path=path or device_file())
    with device.lock():
        device._save(tokens)
    return device


def logout(device: Device, *, local_only: bool = False) -> None:
    """Revoke the device at accounts (with its own device token), then delete the local tokens. If accounts cannot be
    reached the tokens are kept, so the user can retry; `local_only` deletes them without revoking."""
    if not local_only:
        try:
            token = device.access_token()
        except CloudError as e:
            if e.code == "invalid_grant":  # the refresh token is already refused: the device is revoked or expired
                device.forget()
                return
            raise
        with Api(device.accounts_url, transport=device.transport, sleep=device.sleep, clock=device.clock) as api:
            r = api.request("POST", "/v1/device/revoke", headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 204:
            raise fail(r, "logout failed (your local tokens are kept; retry, or use --local): ")
    device.forget()
