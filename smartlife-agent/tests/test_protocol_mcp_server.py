"""真实 MCP Protocol Server 工具发现和调用测试。"""

import asyncio
import json

from app.mcp_servers.protocol_server import build_mcp_server


def test_mcp_server_discovers_current_read_only_tools():
    server = build_mcp_server()
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}

    assert {
        "search_products",
        "get_product_reviews",
        "get_order_status",
        "get_weather",
        "get_route",
        "get_local_activities",
        "get_user_profile",
    } <= names
    assert "save_preference" not in names
    assert "place_order" not in names
    assert "search_hotels" not in names


def test_mcp_server_call_tool_uses_real_repository():
    server = build_mcp_server()
    result = asyncio.run(server.call_tool("search_products", {
        "query": "跑步鞋",
        "max_price": 600,
    }))

    assert result.is_error is False
    text = "".join(item.text for item in result.content if getattr(item, "text", None))
    payload = json.loads(text)
    assert payload
    assert all(row["price"] <= 600 for row in payload)
    assert all(row["source"] == "products_db" for row in payload)


def test_mcp_stdio_transport_end_to_end():
    import sys
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = Path(__file__).resolve().parents[1]

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["scripts/run_mcp_server.py", "--transport", "stdio"],
            cwd=root,
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("get_order_status", {
                    "user_id": "user_001",
                })
                return tools, result

    tools, result = asyncio.run(run())
    names = {tool.name for tool in tools.tools}

    assert "search_products" in names
    assert result.is_error is False
    text = "".join(item.text for item in result.content if getattr(item, "text", None))
    payload = json.loads(text)
    assert payload
    assert all(row["user_id"] == "user_001" for row in payload)


def test_mcp_http_auth_token_verifier_is_optional_and_strict():
    from app.mcp_servers.protocol_server import StaticBearerTokenVerifier, build_mcp_server

    verifier = StaticBearerTokenVerifier("secret-token")
    accepted = asyncio.run(verifier.verify_token("secret-token"))
    rejected = asyncio.run(verifier.verify_token("wrong-token"))
    server = build_mcp_server(auth_token="secret-token")

    assert accepted is not None
    assert accepted.scopes == ["tools"]
    assert rejected is None
    assert server.name == "smartlife-agent"


def test_mcp_server_builds_streamable_http_and_sse_apps():
    server = build_mcp_server(auth_token="secret-token")

    streamable = server.streamable_http_app(host="127.0.0.1")
    sse = server.sse_app(host="127.0.0.1")

    assert streamable is not None
    assert sse is not None
