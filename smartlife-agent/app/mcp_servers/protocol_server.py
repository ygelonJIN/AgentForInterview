"""真正的 MCP Protocol Server，发布统一 Tool Registry 中的工具。"""

from __future__ import annotations

import hmac
import json
from functools import wraps
from typing import Any, Callable

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer

from app.tools.agent_capabilities import get_agent_tool_policy
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
    agent_id: str | None = None,
) -> MCPServer:
    """构建协议级 MCP Server。默认只发布只读工具。

    指定 ``agent_id`` 时构建独立 Agent MCP Server，只发布该 Agent 自己的
    工具能力面；模型或 MCP 客户端都无法通过另一个 Agent 的 Server 调用
    跨域工具。省略 ``agent_id`` 保留旧的统一 Registry Server。
    """
    policy = get_agent_tool_policy(agent_id) if agent_id else None
    registry = get_registry(
        actor_id=actor_id,
        include_write_tools=include_write_tools,
        agent_id=agent_id,
    )
    server = MCPServer(
        name=policy.mcp_server_name if policy else "smartlife-agent",
        title=policy.display_name if policy else "SmartLife Agent Tools",
        description=(
            policy.description if policy
            else "商品、订单、旅游、天气、路线和记忆工具"
        ),
        instructions=(
            f"这是 {policy.display_name} 的独立 MCP 能力面，只允许使用已发布工具。"
            if policy else
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

    if policy is not None:
        # L2：把 Agent 本身作为一个工具暴露。外部系统不必自己拼天气 + 商品，
        # 直接把一句需求交给它；返回正文 + 结构化方案。
        async def run(request: str, user_id: str = "", session_id: str = "mcp") -> str:
            """以自然语言调用本 Agent，返回正文与结构化结果。"""
            from app.agents.runner import run_agent

            actor = (user_id or actor_id or "").strip()
            if not actor:
                return json.dumps({"error": "缺少用户身份：run 需要 user_id 或服务端绑定 actor"}, ensure_ascii=False)
            try:
                payload = await run_agent(
                    policy.agent_id, request, user_id=actor, session_id=session_id
                )
            except Exception as exc:  # 不把内部异常栈透给调用方
                return json.dumps(
                    {"error": f"{type(exc).__name__}: {exc}"[:300]}, ensure_ascii=False
                )
            return json.dumps(payload, ensure_ascii=False, default=str)

        try:
            server.add_tool(
                run,
                name="run",
                description=f"以自然语言调用 {policy.display_name}，返回该 Agent 的完整结果",
                structured_output=False,
            )
        except Exception:
            # 运行时不支持异步工具时，L1 工具面照常可用，不因 L2 起不来。
            pass
    return server
