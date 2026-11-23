"""通用身份服务。"""
from auth_service.app import create_app
from auth_service.config import Settings

__all__ = ["create_app", "Settings"]
