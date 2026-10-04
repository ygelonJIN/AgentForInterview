"""
支付工具 - Function Calling
"""
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import Optional
import uuid
from datetime import datetime

class CreatePaymentInput(BaseModel):
    order_id: str = Field(description="订单ID")
    amount: float = Field(description="支付金额（元）")
    method: str = Field(default="alipay", description="支付方式：alipay/wechat/card")

class GetPaymentStatusInput(BaseModel):
    payment_id: str = Field(description="支付ID")

@tool("create_payment", args_schema=CreatePaymentInput)
def create_payment(order_id: str, amount: float, method: str = "alipay") -> dict:
    """创建支付"""
    payment_id = f"PAY-{uuid.uuid4().hex[:8].upper()}"
    method_names = {"alipay": "支付宝", "wechat": "微信支付", "card": "银行卡"}
    return {
        "payment_id": payment_id,
        "order_id": order_id,
        "amount": amount,
        "method": method_names.get(method, method),
        "status": "success",
        "timestamp": datetime.now().isoformat(),
        "message": f"支付成功！金额 ¥{amount:.2f}，通过{method_names.get(method, method)}支付"
    }

@tool("get_payment_status", args_schema=GetPaymentStatusInput)
def get_payment_status(payment_id: str) -> dict:
    """查询支付状态"""
    return {
        "payment_id": payment_id,
        "status": "success",
        "message": "支付已完成"
    }

def get_payment_tools():
    return [create_payment, get_payment_status]
