"""身份服务测试夹具。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth_service.app import create_app
from auth_service.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "accounts.db",
        jwt_secret="test-secret-key-0123456789abcdef0",
        bcrypt_rounds=4,
        access_ttl=600,
        refresh_ttl=3600,
        allow_register=True,
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))
