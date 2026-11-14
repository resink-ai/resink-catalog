"""The MCP server: lets the user's own AI (Claude Code, Codex) look at the catalog and write back what it learned.

Seven tools, no more. Reading is free; the only writes are tags and notes, and both are stamped `ai`, so the web UI
shows who wrote what. `query_sql` goes through the same read-only path as the web UI (one SELECT, no file access).
Nothing here imports data, drops tables or changes Iceberg tables: an AI can describe your data, not damage it.

Transports: `resink-catalog mcp` (stdio, started by the AI tool itself) and `/mcp` on the local server (HTTP, behind
the same access token as everything else).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field
from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError

from . import __version__
from .catalog import Catalog, ImportErrorForUser
from .query import QueryError

AUTHOR = "ai"
MAX_SAMPLE = 100
MAX_QUERY_ROWS = 500

INSTRUCTIONS = (
    "This is the user's local data catalog: their own CSV and Parquet files turned into Iceberg tables. "
    "Start with list_tables, look at a table with describe_table and sample_table, and use query_sql (read-only SQL, "
    "tables as namespace.table) to check what you think the data means. Ask the user about anything you cannot tell "
    "from the data, then record what you learned with write_note (a table or one column) and add_tags. Your notes "
    "and tags are shown to the user marked as written by ai. You cannot change, import or delete data."
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
ADDITIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _guard(fn: Callable[[], Any]) -> Any:
    """Turn the catalog's user-facing errors into tool errors the AI can read and act on."""
    try:
        return fn()
    except (ImportErrorForUser, QueryError, ValueError) as e:
        raise ToolError(str(e)) from e
    except (NoSuchTableError, NoSuchNamespaceError) as e:
        raise ToolError(f"no such table: {e}") from e


def build_server(get_catalog: Callable[[], Catalog]) -> MCPServer:
    mcp = MCPServer("resink-catalog", instructions=INSTRUCTIONS, version=__version__)

    @mcp.tool(annotations=READ_ONLY)
    def list_tables() -> list[dict]:
        """List every table: name, columns, row count, snapshots and tags (with who wrote each tag)."""
        return _guard(lambda: get_catalog().list_tables())

    @mcp.tool(annotations=READ_ONLY)
    def describe_table(
        table: Annotated[str, Field(description="'namespace.table' (or 'table': default namespace)")],
    ) -> dict:
        """Schema, snapshot history, tags, notes (each marked user or ai), whether it can be pushed to the cloud,
        and the source files it was imported from."""

        def run() -> dict:
            cat = get_catalog()
            info = cat.table_info(table)
            info["sources"] = [
                {"path": s["path"], "status": s["status"], "mode": s["mode"], "rows": s["rows"]}
                for s in cat.sources()
                if s["target_table"] == info["name"]
            ]
            return info

        return _guard(run)

    @mcp.tool(annotations=READ_ONLY)
    def sample_table(
        table: str, limit: Annotated[int, Field(ge=1, le=MAX_SAMPLE, description="rows to return")] = 10
    ) -> dict:
        """The first rows of a table, as column names plus rows."""
        return _guard(lambda: get_catalog().sample(table, limit))

    @mcp.tool(annotations=READ_ONLY)
    def query_sql(
        sql: Annotated[str, Field(description="one read-only SELECT; refer to tables as namespace.table")],
        limit: Annotated[int, Field(ge=1, le=MAX_QUERY_ROWS)] = 100,
    ) -> dict:
        """Run read-only SQL over the tables. Anything that is not a single SELECT is refused (no inserts, updates,
        deletes, DDL, COPY, ATTACH or file access). Returns columns, rows and whether more rows existed."""
        return _guard(lambda: get_catalog().query_json(sql, limit))

    @mcp.tool(annotations=READ_ONLY)
    def list_sources(
        status: Annotated[str | None, Field(description="proposed, imported, changed, error or missing")] = None,
    ) -> list[dict]:
        """The files the catalog knows about: path, format, target table, status, error and row count."""
        keep = ("id", "path", "format", "target_table", "mode", "status", "error", "rows", "last_import")
        return _guard(lambda: [{k: s[k] for k in keep} for s in get_catalog().sources(status)])

    @mcp.tool(annotations=ADDITIVE)
    def add_tags(
        table: str, tags: Annotated[list[str], Field(min_length=1, max_length=20, description="short lowercase labels")]
    ) -> list[dict]:
        """Add tags to a table (for example 'sales', 'customers', 'monthly-export'). Tags already present are kept.
        They are recorded as written by ai. Returns all tags of the table."""
        return _guard(lambda: get_catalog().add_tags(table, tags, source=AUTHOR))

    @mcp.tool(annotations=ADDITIVE)
    def write_note(
        target: Annotated[str, Field(description="'namespace.table', or 'namespace.table.column' for a column")],
        text: Annotated[str, Field(max_length=5000, description="Markdown: what it is, units, quirks, usage")],
    ) -> dict:
        """Record what you learned about a table or a column. Notes are added, never overwritten, and are shown to
        the user marked as written by ai."""
        return _guard(lambda: get_catalog().write_note(target, text, author=AUTHOR))

    return mcp
