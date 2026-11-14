"""L3: the MCP server, over the real transports, with the official SDK client. No network.

Acceptance: (1) an AI can list tables, look at samples, run read-only SQL and write tags and notes back;
(2) what it writes is visible in the web UI's API, marked `ai`; (3) `query_sql` refuses writes.
"""

import sys

import httpx2
import pandas as pd
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from resink_catalog import Catalog
from resink_catalog.mcp_server import build_server

pytestmark = pytest.mark.anyio  # anyio's pytest plugin runs the async tests and fixtures

TOOLS = {"list_tables", "describe_table", "sample_table", "query_sql", "list_sources", "add_tags", "write_note"}


@pytest.fixture
def cat(home):
    c = Catalog(home)
    c.write_table("shop.orders", pd.DataFrame({"id": list(range(30)), "amount": [1.5] * 30, "customer": ["a"] * 30}))
    return c


@pytest.fixture
async def mcp(cat):
    async with Client(build_server(lambda: cat)) as client:
        yield client


def data(result):
    """The JSON a tool returned (the SDK wraps lists in {'result': ...})."""
    import json

    assert not result.is_error, result.content
    if result.structured_content is not None:
        return result.structured_content.get("result", result.structured_content)
    return json.loads(result.content[0].text)


async def test_exactly_the_seven_tools_and_nothing_that_changes_data(mcp):
    tools = (await mcp.list_tools()).tools
    assert {t.name for t in tools} == TOOLS
    for t in tools:  # annotated, so a client can tell reads from additive writes
        assert t.annotations is not None and t.annotations.destructive_hint is False
        assert t.annotations.read_only_hint is (t.name not in ("add_tags", "write_note"))


async def test_list_and_describe_and_sample(mcp):
    [t] = data(await mcp.call_tool("list_tables", {}))
    assert (t["name"], t["rows"], t["columns"]) == ("shop.orders", 30, ["id", "amount", "customer"])
    d = data(await mcp.call_tool("describe_table", {"table": "shop.orders"}))
    assert [f["name"] for f in d["schema"]] == ["id", "amount", "customer"]
    assert d["push_ready"] is True and d["notes"] == [] and d["snapshots"][0]["operation"] == "append"
    s = data(await mcp.call_tool("sample_table", {"table": "shop.orders", "limit": 3}))
    assert s["columns"] == ["id", "amount", "customer"] and len(s["rows"]) == 3


async def test_sample_limit_is_capped(mcp):
    r = await mcp.call_tool("sample_table", {"table": "shop.orders", "limit": 5000})
    assert r.is_error  # the schema refuses more than 100 rows


async def test_query_sql_reads(mcp):
    r = data(await mcp.call_tool("query_sql", {"sql": "select count(*) as n, sum(amount) as total from shop.orders"}))
    assert r == {"columns": ["n", "total"], "rows": [[30, 45.0]], "truncated": False}
    r = data(await mcp.call_tool("query_sql", {"sql": "select id from shop.orders order by id", "limit": 5}))
    assert len(r["rows"]) == 5 and r["truncated"] is True


@pytest.mark.parametrize(
    "sql",
    [
        "drop table shop.orders",
        "delete from shop.orders",
        "insert into shop.orders values (1, 1.0, 'x')",
        "update shop.orders set amount = 0",
        "create table x as select 1",
        "select 1; drop table shop.orders",
        "copy shop.orders to '/tmp/x.csv'",
        "attach '/tmp/x.db'",
        "install httpfs",
        "select * from read_csv('/etc/passwd')",
    ],
)
async def test_query_sql_refuses_writes_and_the_data_is_untouched(mcp, cat, sql):
    r = await mcp.call_tool("query_sql", {"sql": sql})
    assert r.is_error
    assert cat.list_tables()[0]["rows"] == 30
    assert (
        cat.table_info("shop.orders")["snapshots"][0]["operation"] == "append"
        and len(cat.table_info("shop.orders")["snapshots"]) == 1
    )


async def test_tags_and_notes_are_written_as_ai(mcp, cat):
    tags = data(await mcp.call_tool("add_tags", {"table": "shop.orders", "tags": ["Sales", "monthly-export"]}))
    assert {(t["tag"], t["source"]) for t in tags} == {("sales", "ai"), ("monthly-export", "ai")}
    note = data(await mcp.call_tool("write_note", {"target": "shop.orders", "text": "One row per order."}))
    col = data(await mcp.call_tool("write_note", {"target": "shop.orders.amount", "text": "EUR, VAT included"}))
    assert note["author"] == col["author"] == "ai" and col["target"] == "shop.orders.amount"
    d = data(await mcp.call_tool("describe_table", {"table": "shop.orders"}))
    assert [(n["target"], n["author"]) for n in d["notes"]] == [("shop.orders", "ai"), ("shop.orders.amount", "ai")]
    [t] = data(await mcp.call_tool("list_tables", {}))
    assert {x["tag"]: x["source"] for x in t["tags"]} == {"sales": "ai", "monthly-export": "ai"}
    # the user's own tag is not overwritten by the AI re-adding it
    cat.add_tags("shop.orders", ["finance"], source="user")
    tags = data(await mcp.call_tool("add_tags", {"table": "shop.orders", "tags": ["finance"]}))
    assert {t["tag"]: t["source"] for t in tags}["finance"] == "user"


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("add_tags", {"table": "nope.nope", "tags": ["x"]}),
        ("add_tags", {"table": "shop.orders", "tags": [" "]}),
        ("add_tags", {"table": "shop.orders", "tags": ["x" * 65]}),
        ("write_note", {"target": "nope.nope", "text": "x"}),
        ("write_note", {"target": "shop.orders.nosuchcolumn", "text": "x"}),
        ("write_note", {"target": "shop.orders", "text": "   "}),
        ("write_note", {"target": "shop.orders", "text": "x" * 5001}),
        ("describe_table", {"table": "nope.nope"}),
        ("sample_table", {"table": "nope.nope"}),
    ],
)
async def test_bad_arguments_are_tool_errors_the_ai_can_read(mcp, tool, args):
    r = await mcp.call_tool(tool, args)
    assert r.is_error and r.content[0].text


async def test_list_sources(mcp, cat, tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("x\n1\n")
    cat.import_file(f, "default.a")
    [s] = data(await mcp.call_tool("list_sources", {}))
    assert (s["target_table"], s["status"], s["rows"]) == ("default.a", "imported", 1)
    assert data(await mcp.call_tool("list_sources", {"status": "error"})) == []
    d = data(await mcp.call_tool("describe_table", {"table": "default.a"}))
    assert d["sources"][0]["path"] == str(f.resolve())


# ---------------------------------------------------------------------------------- the real transports


async def test_over_stdio_a_subprocess_serves_the_same_tools_and_writes_ai_notes(cat, home):
    """What Claude Code does: start `resink-catalog mcp` and talk on stdin/stdout."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "resink_catalog.cli", "mcp", "--home", str(home)]
    )
    async with Client(params) as client:
        assert {t.name for t in (await client.list_tools()).tools} == TOOLS
        [t] = data(await client.call_tool("list_tables", {}))
        assert t["name"] == "shop.orders"
        assert data(await client.call_tool("query_sql", {"sql": "select count(*) as n from shop.orders"}))["rows"] == [
            [30]
        ]
        assert (await client.call_tool("query_sql", {"sql": "drop table shop.orders"})).is_error
        data(await client.call_tool("add_tags", {"table": "shop.orders", "tags": ["orders"]}))
        data(await client.call_tool("write_note", {"target": "shop.orders", "text": "Written over stdio."}))
    # the subprocess is gone; the rows are in the catalog file, written by ai
    fresh = Catalog(home)
    assert [(n["body"], n["author"]) for n in fresh.list_notes("shop.orders")] == [("Written over stdio.", "ai")]
    assert fresh.list_tags("shop.orders")[0]["source"] == "ai"


async def test_over_http_the_token_is_required(live_server, cat):
    url = f"{live_server.url}/mcp"
    assert httpx2.post(url, json={}).status_code == 401
    assert httpx2.post(url, json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {live_server.token}"})
    async with Client(streamable_http_client(url, http_client=http)) as client:
        assert {t.name for t in (await client.list_tools()).tools} == TOOLS
        data(await client.call_tool("write_note", {"target": "shop.orders", "text": "Over HTTP."}))
        assert (await client.call_tool("query_sql", {"sql": "delete from shop.orders"})).is_error
    await http.aclose()
    assert [n["author"] for n in Catalog(live_server.home).list_notes("shop.orders")] == ["ai"]
