"""支付工具已移除。

项目只提供购物搜索、评价查询和只读订单查询，不提供创建支付能力。
保留此模块是为了兼容旧 import，不注册任何支付工具。
"""
from typing import List


def get_payment_tools() -> List:
    """返回空工具列表：支付功能已禁用。"""
    return []


__all__ = ["get_payment_tools"]
