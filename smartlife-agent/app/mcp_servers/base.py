"""
MCP Server 基类
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

class BaseMCPServer(ABC):
    """
    MCP Server 基类
    
    所有子Agent的MCP Server都应该继承这个基类
    """
    
    def __init__(self, name: str, description: str):
        """
        初始化MCP Server
        
        Args:
            name: Server名称
            description: Server描述
        """
        self.name = name
        self.description = description
        self.tools: List[BaseTool] = []
        self._initialize_tools()
    
    @abstractmethod
    def _initialize_tools(self):
        """初始化工具列表，子类必须实现"""
        pass
    
    def get_tools(self) -> List[BaseTool]:
        """获取所有工具"""
        return self.tools
    
    def get_tool_by_name(self, name: str) -> Optional[BaseTool]:
        """根据名称获取工具"""
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None
    
    def get_tool_descriptions(self) -> List[Dict[str, str]]:
        """获取所有工具的描述"""
        return [
            {
                "name": tool.name,
                "description": tool.description
            }
            for tool in self.tools
        ]
    
    def get_server_info(self) -> Dict[str, Any]:
        """获取Server信息"""
        return {
            "name": self.name,
            "description": self.description,
            "tools": self.get_tool_descriptions()
        }
