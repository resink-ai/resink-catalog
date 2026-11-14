"""FastAPI app. Every route except /healthz requires the access token."""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError

from . import __version__
from .catalog import Catalog, ImportErrorForUser
from .iceberg_rest import build_router
from .query import QueryError

PUBLIC_PATHS = frozenset({"/healthz"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    # The browser is opened on a link carrying the token (design section 4).
    return request.query_params.get("token")


class QueryBody(BaseModel):
    sql: str
    limit: int = 1000


class RootBody(BaseModel):
    path: str


class ImportBody(BaseModel):
    root: str | None = None  # apply the proposal for this folder
    file: str | None = None  # or import one file
    table: str | None = None


class ScanBody(BaseModel):
    apply: bool = False


def create_app(token: str, home: Path | str | None = None) -> FastAPI:
    app = FastAPI(title="Resink Local Catalog", version=__version__, docs_url=None, redoc_url=None)
    app.state.catalog = None

    def catalog() -> Catalog:
        if app.state.catalog is None:  # opened on first use, so /healthz and /api/v1/info never touch the disk
            app.state.catalog = Catalog(home)
        return app.state.catalog

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

    app.include_router(build_router(lambda: catalog().iceberg))

    @app.exception_handler(ImportErrorForUser)
    def _user_error(request: Request, exc: ImportErrorForUser):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(QueryError)
    def _query_error(request: Request, exc: QueryError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/v1/tables")
    def tables() -> list[dict]:
        return catalog().list_tables()

    @app.get("/api/v1/tables/{name}")
    def table(name: str) -> dict:
        try:
            return catalog().table_info(name)
        except (NoSuchTableError, NoSuchNamespaceError):
            raise HTTPException(404, f"no such table: {name}") from None

    @app.post("/api/v1/query")
    def query(body: QueryBody) -> dict:
        return catalog().query_json(body.sql, body.limit)

    @app.get("/api/v1/sources")
    def sources(status: str | None = None) -> list[dict]:
        return catalog().sources(status)

    @app.post("/api/v1/roots")
    def add_root(body: RootBody) -> list[dict]:
        """Propose: nothing is imported until POST /api/v1/sources/import."""
        return catalog().add_folder(body.path)

    @app.post("/api/v1/sources/import")
    def import_sources(body: ImportBody) -> list[dict]:
        if bool(body.root) == bool(body.file):
            raise HTTPException(400, "give exactly one of `root` or `file`")
        if body.root:
            return [r.as_dict() for r in catalog().import_folder(body.root)]
        return [catalog().import_file(body.file, body.table).as_dict()]

    @app.post("/api/v1/sources/scan")
    def scan(body: ScanBody | None = None) -> dict:
        return catalog().scan(apply=bool(body and body.apply))

    return app
