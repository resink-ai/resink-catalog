"""Write a tag and a note into a catalog through the real MCP stdio server, as an AI client would.

    uv run python tests/mcp_seed.py <catalog home> <table>

Used by the Playwright smoke test, which then checks that the web UI shows both marked `ai`.
"""

import sys

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def main(home: str, table: str) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "resink_catalog.cli", "mcp", "--home", home])
    async with Client(params) as client:
        for call in (
            ("add_tags", {"table": table, "tags": ["monthly-export", "finance"]}),
            ("write_note", {"target": table, "text": "Monthly sales export; one row per invoice line."}),
        ):
            result = await client.call_tool(*call)
            assert not result.is_error, result.content


if __name__ == "__main__":
    anyio.run(main, sys.argv[1], sys.argv[2])
