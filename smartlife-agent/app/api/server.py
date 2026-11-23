"""项目 API 进程：身份服务 + 本项目只读适配接口。

启动：``uvicorn app.api.server:create_server --factory --port 8000``
"""
from __future__ import annotations

from pathlib import Path

from auth_service.app import create_app
from auth_service.config import Settings

from app.api.me import router as me_router

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRODUCTS_DB = PROJECT_ROOT / "data" / "products.db"


def create_server(
    settings: Settings | None = None,
    products_db: Path | str | None = None,
):
    # 不管从哪个目录启动，账号库和密钥都落在项目 data/ 下。
    application = create_app(settings or Settings.from_env(base_dir=PROJECT_ROOT))
    application.state.products_db = Path(products_db or DEFAULT_PRODUCTS_DB)
    application.include_router(me_router)
    return application
