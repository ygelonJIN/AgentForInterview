"""真正的 MCP Protocol Server，发布统一 Tool Registry 中的工具。"""

from __future__ import annotations

import hmac
import json
from functools import wraps
from typing import Any, Callable

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer

from app.tools.registry import get_registry


class StaticBearerTokenVerifier:
    """本地/自托管部署使用的固定 Bearer Token 验证器。"""

    def __init__(self, token: str, resource: str = "http://127.0.0.1:8765"):
        if not token:
            raise ValueError("MCP auth token 不能为空")
        self._token = token
        self._resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(str(token or ""), self._token):
            return None
        return AccessToken(
            token=token,
            client_id="smartlife-mcp-client",
            scopes=["tools"],
            subject="local-client",
            resource=self._resource,
        )


def _mcp_callable(tool: Any) -> Callable[..., Any]:
    func = getattr(tool, "func", None)
    if callable(func):
        @wraps(func)
        def wrapped(*args: Any, **kwargs: Any) -> str:
            return json.dumps(func(*args, **kwargs), ensure_ascii=False, default=str)

        return wrapped

    def invoke(**arguments: Any) -> str:
        return json.dumps(tool.invoke(arguments), ensure_ascii=False, default=str)

    invoke.__name__ = tool.name
    invoke.__doc__ = tool.description or tool.name
    return invoke


def build_mcp_server(
    *,
    include_write_tools: bool = False,
    auth_token: str | None = None,
    public_url: str = "http://127.0.0.1:8765",
    actor_id: str | None = None,
) -> MCPServer:
    """构建协议级 MCP Server。默认只发布只读工具。"""
    registry = get_registry(actor_id=actor_id, include_write_tools=include_write_tools)
    server = MCPServer(
        name="smartlife-agent",
        title="SmartLife Agent Tools",
        description="商品、订单、旅游、天气、路线和记忆工具",
        instructions=(
            "所有订单和记忆数据都受用户身份约束。"
            "记忆写入工具只会创建审批请求，不会绕过用户审批。"
        ),
        version="1.0.0",
        warn_on_duplicate_tools=True,
        token_verifier=(
            StaticBearerTokenVerifier(auth_token, resource=public_url)
            if auth_token
            else None
        ),
        auth=(
            AuthSettings(
                issuer_url=public_url,
                resource_server_url=public_url,
                required_scopes=["tools"],
                validate_token_resource=False,
            )
            if auth_token
            else None
        ),
    )
    for tool in registry.tools():
        server.add_tool(
            _mcp_callable(tool),
            name=tool.name,
            description=tool.description or tool.name,
            structured_output=False,
        )
    return server


async def list_tool_names(*, include_write_tools: bool = False) -> list[str]:
    server = build_mcp_server(include_write_tools=include_write_tools)
    return [tool.name for tool in await server.list_tools()]
