"""FastAPI app. The API, Iceberg and MCP routes need the access token in an Authorization header. The web page's own
files (HTML, JS, CSS) carry no data and are served without it: the browser opens the link with the token in the URL
fragment, which is never sent to any server, and the page sends the token as a header."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError

from . import __version__
from .catalog import Catalog, ImportErrorForUser
from .iceberg_rest import build_router
from .mcp_server import build_server
from .query import QueryError

PUBLIC_PATHS = frozenset({"/healthz"})
PROTECTED_PREFIXES = ("/api/", "/iceberg/", "/mcp")  # everything else is the page's own static files
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})


STATIC_DIR = Path(__file__).parent / "static"
NO_UI = (
    "<!doctype html><meta charset=utf-8><title>Resink Catalog</title>"
    "<body style='font:16px system-ui;max-width:40rem;margin:3rem auto;padding:0 1rem'>"
    "<h1>Resink Catalog</h1><p>The web UI is not built in this install. From a source checkout run "
    "<code>scripts/build_ui.sh</code>, or install the released package. The API works: "
    "<code>/api/v1/tables</code>, <code>/iceberg/v1/config</code>.</p>"
)


def _bearer(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value else None


class QueryBody(BaseModel):
    sql: str
    limit: int = 1000


class RootBody(BaseModel):
    path: str


class ImportBody(BaseModel):
    root: str | None = None  # apply the proposal for this folder
    file: str | None = None  # or import one file
    source_id: int | None = None  # or import one source of a proposal
    table: str | None = None
    column_types: dict[str, str] | None = None  # override inferred types (file or source_id only)


class TagsBody(BaseModel):
    tags: list[str]


class NoteBody(BaseModel):
    target: str | None = None  # the table itself, or 'ns.table.column'; defaults to the table in the URL
    body: str


class TypesBody(BaseModel):
    column_types: dict[str, str]


class ScanBody(BaseModel):
    apply: bool = False


def create_app(token: str, home: Path | str | None = None) -> FastAPI:
    mcp_app = None  # built below, after `catalog` exists; its session manager must run inside our lifespan

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = FastAPI(title="Resink Local Catalog", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.catalog = None

    def catalog() -> Catalog:
        if app.state.catalog is None:  # opened on first use, so /healthz and /api/v1/info never touch the disk
            app.state.catalog = Catalog(home)
        return app.state.catalog

    mcp_app = build_server(catalog).streamable_http_app(streamable_http_path="/")

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if request.scope["path"] == "/mcp":
            request.scope["path"] = "/mcp/"  # the MCP app is mounted at /mcp; do not bounce POSTs through a redirect
        # Applies to every path, including ones that do not exist, so probing reveals nothing.
        host = request.headers.get("host", "").rsplit(":", 1)[0]
        if host not in LOOPBACK_HOSTS:  # blocks DNS rebinding
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        path = request.url.path
        if path not in PUBLIC_PATHS and (path + "/").startswith(PROTECTED_PREFIXES):
            supplied = _bearer(request)
            if supplied is None or not secrets.compare_digest(supplied, token):
                return JSONResponse(
                    {"detail": "missing or invalid access token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            origin = request.headers.get("origin")
            if origin and request.method not in ("GET", "HEAD"):
                if origin.split("://", 1)[-1] != request.headers.get("host"):  # a page on another port or site
                    return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/api/v1/info")
    def info() -> dict:
        return {"name": "resink-catalog", "version": __version__}

    app.include_router(build_router(lambda: catalog().iceberg))
    app.mount("/mcp", mcp_app)  # MCP over HTTP, behind the same token as every other route

    @app.exception_handler(ImportErrorForUser)
    def _user_error(request: Request, exc: ImportErrorForUser):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(QueryError)
    def _query_error(request: Request, exc: QueryError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/v1/tables")
    def tables() -> list[dict]:
        return catalog().list_tables()

    @app.get("/api/v1/tables/{name}/sample")
    def table_sample(name: str, limit: int = 20) -> dict:
        try:
            return catalog().sample(name, max(1, min(limit, 1000)))
        except (NoSuchTableError, NoSuchNamespaceError):
            raise HTTPException(404, f"no such table: {name}") from None

    @app.get("/api/v1/tables/{name}")
    def table(name: str, notes_after: int = 0, notes_limit: int = 50) -> dict:
        try:
            return catalog().table_info(name, notes_after, notes_limit)
        except (NoSuchTableError, NoSuchNamespaceError):
            raise HTTPException(404, f"no such table: {name}") from None

    @app.post("/api/v1/query")
    def query(body: QueryBody) -> dict:
        return catalog().query_json(body.sql, body.limit)

    @app.get("/api/v1/sources")
    def sources(status: str | None = None) -> list[dict]:
        return catalog().sources(status)

    @app.post("/api/v1/tables/{name}/tags")
    def add_tags(name: str, body: TagsBody) -> list[dict]:
        return catalog().add_tags(name, body.tags, source="user")

    @app.delete("/api/v1/tags/{tag_id}")
    def remove_tag(tag_id: int) -> dict:
        if not catalog().remove_tag(tag_id):
            raise HTTPException(404, f"no such tag: {tag_id}")
        return {"removed": tag_id}

    @app.post("/api/v1/tables/{name}/notes")
    def add_note(name: str, body: NoteBody) -> dict:
        return catalog().write_note(body.target or name, body.body, author="user")

    @app.delete("/api/v1/notes/{note_id}")
    def delete_note(note_id: int) -> dict:
        if not catalog().delete_note(note_id):
            raise HTTPException(404, f"no such note: {note_id}")
        return {"removed": note_id}

    @app.get("/api/v1/roots")
    def roots() -> list[dict]:
        return catalog().roots()

    @app.get("/api/v1/sources/{source_id}/preview")
    def source_preview(source_id: int, limit: int = 20) -> dict:
        return catalog().preview(source_id, max(1, min(limit, 200)))

    @app.post("/api/v1/sources/{source_id}/types")
    def source_types(source_id: int, body: TypesBody) -> dict:
        return catalog().set_types(source_id, body.column_types)

    @app.post("/api/v1/roots")
    def add_root(body: RootBody) -> list[dict]:
        """Propose: nothing is imported until POST /api/v1/sources/import."""
        return catalog().add_folder(body.path)

    @app.post("/api/v1/sources/import")
    def import_sources(body: ImportBody) -> list[dict]:
        if sum(x is not None and x != "" for x in (body.root, body.file, body.source_id)) != 1:
            raise HTTPException(400, "give exactly one of `root`, `file` or `source_id`")
        if body.root:
            if body.column_types:
                raise HTTPException(400, "column_types apply to one file or source, not to a folder")
            return [r.as_dict() for r in catalog().import_folder(body.root)]
        if body.source_id is not None:
            if body.column_types is not None:
                catalog().set_types(body.source_id, body.column_types)
            return [catalog().import_source(body.source_id).as_dict()]
        return [catalog().import_file(body.file, body.table, body.column_types).as_dict()]

    @app.post("/api/v1/sources/scan")
    def scan(body: ScanBody | None = None) -> dict:
        return catalog().scan(apply=bool(body and body.apply))

    index = STATIC_DIR / "index.html"
    if (STATIC_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def ui(path: str):
        """The single-page app. API and Iceberg paths that matched nothing stay 404 JSON."""
        if path.startswith(("api/", "iceberg/")):
            raise HTTPException(404, "not found")
        if not index.exists():
            return HTMLResponse(NO_UI)
        candidate = (STATIC_DIR / path).resolve()
        if path and candidate.is_file() and STATIC_DIR.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)  # client-side routes

    return app
