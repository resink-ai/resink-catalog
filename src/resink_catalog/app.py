"""FastAPI app. Every route except /healthz requires the access token."""

from __future__ import annotations

import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__

PUBLIC_PATHS = frozenset({"/healthz"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    # The browser is opened on a link carrying the token (design section 4).
    return request.query_params.get("token")


def create_app(token: str) -> FastAPI:
    app = FastAPI(title="Resink Local Catalog", version=__version__, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        # Applies to every path, including ones that do not exist, so probing reveals nothing.
        host = request.headers.get("host", "").rsplit(":", 1)[0]
        if host not in LOOPBACK_HOSTS:  # blocks DNS rebinding
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        if request.url.path not in PUBLIC_PATHS:
            supplied = _extract_token(request)
            if supplied is None or not secrets.compare_digest(supplied, token):
                return JSONResponse(
                    {"detail": "missing or invalid access token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/api/v1/info")
    def info() -> dict:
        return {"name": "resink-catalog", "version": __version__}

    return app
