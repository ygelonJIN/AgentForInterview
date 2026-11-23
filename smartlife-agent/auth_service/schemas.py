"""接口的请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    username: str = Field(description="登录名，3—32 个字符")
    password: str = Field(description="密码，至少 8 位")
    display_name: str | None = Field(default=None, description="展示名，默认与用户名相同")
    invite_code: str | None = Field(default=None, description="开启邀请制时必填")


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    username: str
    password: str


class RefreshIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    refresh_token: str


class LogoutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    refresh_token: str | None = None


class ChangePasswordIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    old_password: str
    new_password: str


class AccountOut(BaseModel):
    id: str
    username: str
    display_name: str
    roles: list[str]
    scopes: list[str]
    status: str
    created_at: str
    last_login_at: str | None = None


class TokenPairOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_expires_in: int
    refresh_token: str
    account: AccountOut
