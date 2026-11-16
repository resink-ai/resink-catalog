"""HTTP to the cloud: problem documents shown verbatim, and retries of 429 and 503 (push protocol section 8).

Client behaviour from the design: retry `429` and `503` with exponential backoff, up to 5 minutes in total, honouring
`Retry-After`; never retry another 4xx (the callers handle `409 offset_mismatch` themselves); print the problem's
`title`, `detail` and first `resolution` verbatim. A lost connection is retried within the same budget: every push call
is safe to repeat (sessions are idempotent on their key, a repeated chunk gets `409 offset_mismatch`, commit is
idempotent). Token exchanges are not: they are retried only when the connection could not be made at all.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpcore2
import httpx2

RETRY_STATUSES = (429, 503)
RETRY_BUDGET_SECONDS = 300.0
MAX_DELAY_SECONDS = 60.0
TIMEOUT = httpx2.Timeout(60.0, connect=15.0)


class CloudError(Exception):
    """A refusal the user must read. `problem` is the RFC 9457 document (or an equivalent built by the client)."""

    def __init__(self, message: str, problem: dict | None = None, status: int | None = None):
        super().__init__(message)
        self.problem = problem or {}
        self.status = status

    @property
    def code(self) -> str | None:
        return self.problem.get("code")

    def lines(self) -> list[str]:
        return problem_lines(self.problem) if self.problem.get("title") else [f"error: {self}"]


class RequestMaybeSent(CloudError):
    """The request may have reached the server, but no complete answer came back: its outcome is unknown."""


def problem_lines(problem: dict) -> list[str]:
    """`title`, `detail` and the first `resolution`, verbatim."""
    lines = [f"error: {problem.get('title', 'error')}" + (f" ({problem['code']})" if problem.get("code") else "")]
    if problem.get("detail"):
        lines.append(str(problem["detail"]))
    resolutions = problem.get("resolution") or []
    if resolutions:
        r = resolutions[0]
        lines.append("resolution: " + " ".join(str(r[k]) for k in ("action", "detail", "url") if r.get(k)))
    return lines


def problem_of(r: httpx2.Response) -> dict:
    """The problem document of an error response. Accounts' token endpoint answers OAuth errors
    (`{"error", "error_description"}`, RFC 6749 section 5.2); they are mapped onto the same fields."""
    try:
        body = r.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and "title" in body:
        return body
    if isinstance(body, dict) and "error" in body:
        return {
            "title": body["error"],
            "code": body["error"],
            "detail": body.get("error_description", ""),
            "status": r.status_code,
        }
    return {"title": f"HTTP {r.status_code}", "status": r.status_code, "detail": r.text[:500]}


def fail(r: httpx2.Response, context: str = "") -> CloudError:
    p = problem_of(r)
    return CloudError(f"{context}{p.get('title')}: {p.get('detail', '')}", p, r.status_code)


def retry_after(r: httpx2.Response) -> float | None:
    try:
        return max(0.0, float(r.headers["Retry-After"]))
    except (KeyError, ValueError):
        return None


LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def check_url(url: str) -> str:
    """Credentials only travel over https; plain http is allowed to a loopback host only (tests, local development)."""
    u = urlsplit(url)
    if u.scheme == "https" and u.hostname:
        return url.rstrip("/")
    if u.scheme == "http" and u.hostname in LOOPBACK_HOSTS:
        return url.rstrip("/")
    raise CloudError(f"refusing cloud address {url!r}: it must be https:// (http:// only to 127.0.0.1 or localhost)")


class Backoff:
    """Exponential backoff (1, 2, 4 ... 60 seconds, or the server's Retry-After if longer) within a deadline that
    also counts the time spent in requests (`clock` is monotonic)."""

    def __init__(
        self, sleep: Callable[[float], None], clock: Callable[[], float], budget: float = RETRY_BUDGET_SECONDS
    ):
        self.sleep, self.clock, self.n = sleep, clock, 0
        self.deadline = clock() + budget

    def remaining(self) -> float:
        return self.deadline - self.clock()

    def wait(self, server_delay: float | None) -> bool:
        delay = max(server_delay or 0.0, min(2.0**self.n, MAX_DELAY_SECONDS))
        if delay >= self.remaining():
            return False
        self.sleep(delay)
        self.n += 1
        return self.remaining() > 0


class _Cuttable(httpcore2.NetworkStream):
    """A network stream whose socket the owner can shut down from another thread."""

    def __init__(self, inner: httpcore2.NetworkStream, backend: _CuttingBackend):
        self._inner, self._backend = inner, backend
        backend.streams.add(self)

    def read(self, max_bytes, timeout=None):
        return self._inner.read(max_bytes, timeout)

    def write(self, buffer, timeout=None):
        self._inner.write(buffer, timeout)

    def close(self):
        self._backend.streams.discard(self)
        self._inner.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self._backend.streams.discard(self)  # the TLS stream owns the socket from now on
        return _Cuttable(self._inner.start_tls(ssl_context, server_hostname, timeout), self._backend)

    def get_extra_info(self, info):
        return self._inner.get_extra_info(info)

    def cut(self) -> None:
        sock = self._inner.get_extra_info("socket")
        try:
            sock.shutdown(socket.SHUT_RDWR)  # wakes a read blocked in another thread; close() alone does not
        except (OSError, AttributeError):
            pass


class _CuttingBackend(httpcore2.NetworkBackend):
    """httpcore's own blocking backend, keeping track of the open streams so that `cut_all` can stop them."""

    def __init__(self):
        self._inner = httpcore2.SyncBackend()
        self.streams: set[_Cuttable] = set()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return _Cuttable(self._inner.connect_tcp(host, port, timeout, local_address, socket_options), self)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):  # pragma: no cover - not used
        return _Cuttable(self._inner.connect_unix_socket(path, timeout, socket_options), self)

    def sleep(self, seconds):  # pragma: no cover - used by httpcore retries only
        self._inner.sleep(seconds)

    def cut_all(self) -> None:
        for stream in list(self.streams):
            stream.cut()


class Api:
    """One cloud service. `token` gives the current bearer token; `refresh` gets a new one after a 401 (once)."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx2.BaseTransport | None = None,
        token: Callable[[], str] | None = None,
        refresh: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        budget: float = RETRY_BUDGET_SECONDS,
    ):
        self.base_url = check_url(base_url)
        self._transport = transport
        self.token, self.refresh, self.sleep, self.clock = token, refresh, sleep, clock
        self.budget = budget
        self._new_client()

    def _new_client(self) -> None:
        """A client whose connections can be cut from another thread (network transport only; a transport passed
        in by a caller, such as a test's fake, is reused as it is)."""
        self._backend: _CuttingBackend | None = None
        transport = self._transport
        if transport is None:
            transport = httpx2.HTTPTransport()
            pool = getattr(transport, "_pool", None)
            if pool is not None and hasattr(pool, "_network_backend"):  # httpcore's ConnectionPool
                self._backend = _CuttingBackend()
                pool._network_backend = self._backend
        self.client = httpx2.Client(base_url=self.base_url, transport=transport, timeout=TIMEOUT)

    def _abandon(self, worker: threading.Thread) -> None:
        """Cancel a request that ran past its deadline: shut its sockets down (so the worker's blocked read fails
        and the thread exits), close the client, and use a fresh one for later calls."""
        old, backend = self.client, self._backend
        if backend is not None:
            backend.cut_all()
        try:
            old.close()
        except Exception:  # noqa: BLE001 - closing under a failing request may raise; the client is discarded
            pass
        self._new_client()
        worker.join(0.2)  # usually enough for the cut read to fail; never wait long on a transport that cannot be cut

    def _send(self, limit: float, method: str, path: str, **kw) -> httpx2.Response:
        """One request, headers and body read in full, within an absolute `limit` in seconds. Per-read timeouts
        alone do not bound a response that trickles bytes, so the request runs in a worker thread; when the limit
        passes, its connection is cut and the client replaced (`_abandon`)."""
        out: dict = {}

        def run() -> None:
            try:
                out["response"] = self.client.request(method, path, **kw)  # .request reads the whole body
            except BaseException as e:  # noqa: BLE001 - re-raised in the caller's thread
                out["error"] = e

        worker = threading.Thread(target=run, daemon=True, name="resink-catalog-http")
        worker.start()
        worker.join(max(limit, 0.0))
        if worker.is_alive():
            self._abandon(worker)
            raise RequestMaybeSent(f"no complete answer from {self.base_url} within the time limit")
        if "error" in out:
            raise out["error"]
        return out["response"]

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Api:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def request(
        self, method: str, path: str, *, headers: dict | None = None, once: bool = False, **kw
    ) -> httpx2.Response:
        """`once`: for requests that must not be repeated after they may have reached the server (token exchanges:
        a code is single use, and a resent refresh token counts as reuse and revokes the device). Only a failure to
        connect, where nothing was sent, is retried."""
        backoff = Backoff(self.sleep, self.clock, self.budget)
        refreshed = False
        while True:
            h = dict(headers or {})
            sent_token = None
            if self.token is not None:
                sent_token = self.token()
                h["Authorization"] = f"Bearer {sent_token}"
            remaining = backoff.remaining()
            if remaining <= 0:
                raise CloudError(f"gave up on {self.base_url} after {self.budget:.0f} seconds")
            timeout = httpx2.Timeout(min(TIMEOUT.read, remaining), connect=min(TIMEOUT.connect, remaining))
            try:
                r = self._send(remaining, method, path, headers=h, timeout=timeout, **kw)
            except (httpx2.ConnectError, httpx2.ConnectTimeout) as e:
                if backoff.wait(None):
                    continue
                raise CloudError(f"cannot reach {self.base_url}: {e}") from e
            except httpx2.TransportError as e:
                if not once and backoff.wait(None):
                    continue
                raise RequestMaybeSent(f"lost the connection to {self.base_url}: {e}") from e
            if not once and r.status_code in RETRY_STATUSES and backoff.wait(retry_after(r)):
                continue
            if r.status_code == 401 and self.refresh is not None and not refreshed:
                refreshed = True
                self.refresh(sent_token)
                continue
            return r
