"""领域工具集合的轻量容器。

这里只负责组织 LangChain BaseTool，不实现或模拟 MCP 协议。
真正的 MCP Protocol Server 位于 ``app.mcp_servers.protocol_server``。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from langchain_core.tools import BaseTool


class ToolBundle:
    """一组按业务领域组织的工具。"""

    def __init__(
        self,
        name: str,
        description: str,
        tools: Optional[Iterable[BaseTool]] = None,
    ):
        self.name = name
        self.description = description
        self.tools: List[BaseTool] = list(tools or [])
        if tools is None:
            initializer = getattr(self, "_initialize_tools", None)
            if callable(initializer):
                initializer()

    def get_tools(self) -> List[BaseTool]:
        return list(self.tools)

    def get_tool_by_name(self, name: str) -> Optional[BaseTool]:
        return next((tool for tool in self.tools if tool.name == name), None)

    def describe(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "args_schema": getattr(tool, "args_schema", None).__name__
                if getattr(tool, "args_schema", None) is not None
                else None,
            }
            for tool in self.tools
        ]
