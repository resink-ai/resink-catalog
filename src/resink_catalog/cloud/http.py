"""HTTP to the cloud: problem documents shown verbatim, and retries of 429 and 503 (push protocol section 8).

Client behaviour from the design: retry `429` and `503` with exponential backoff, up to 5 minutes in total, honouring
`Retry-After`; never retry another 4xx (the callers handle `409 offset_mismatch` themselves); print the problem's
`title`, `detail` and first `resolution` verbatim. A lost connection is retried within the same budget: every call the
client makes is safe to repeat (sessions are idempotent on their key, a repeated chunk gets `409 offset_mismatch`,
commit is idempotent).
"""

from __future__ import annotations

import time
from collections.abc import Callable

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


class Backoff:
    """Exponential backoff (1, 2, 4 ... 60 seconds, or the server's Retry-After if longer) within a total budget."""

    def __init__(self, sleep: Callable[[float], None], budget: float = RETRY_BUDGET_SECONDS):
        self.sleep, self.budget, self.spent, self.n = sleep, budget, 0.0, 0

    def wait(self, server_delay: float | None) -> bool:
        delay = max(server_delay or 0.0, min(2.0**self.n, MAX_DELAY_SECONDS))
        if self.spent + delay > self.budget:
            return False
        self.sleep(delay)
        self.spent += delay
        self.n += 1
        return True


class Api:
    """One cloud service. `token` gives the current bearer token; `refresh` gets a new one after a 401 (once)."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx2.BaseTransport | None = None,
        token: Callable[[], str] | None = None,
        refresh: Callable[[], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.client = httpx2.Client(base_url=self.base_url, transport=transport, timeout=TIMEOUT)
        self.token, self.refresh, self.sleep = token, refresh, sleep

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Api:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def request(self, method: str, path: str, *, headers: dict | None = None, **kw) -> httpx2.Response:
        backoff = Backoff(self.sleep)
        refreshed = False
        while True:
            h = dict(headers or {})
            if self.token is not None:
                h["Authorization"] = f"Bearer {self.token()}"
            try:
                r = self.client.request(method, path, headers=h, **kw)
            except httpx2.TransportError as e:
                if backoff.wait(None):
                    continue
                raise CloudError(f"cannot reach {self.base_url}: {e}") from e
            if r.status_code in RETRY_STATUSES and backoff.wait(retry_after(r)):
                continue
            if r.status_code == 401 and self.refresh is not None and not refreshed:
                refreshed = True
                self.refresh()
                continue
            return r
