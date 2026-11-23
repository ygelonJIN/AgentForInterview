"""FastAPI 应用工厂。

既可以单独跑（``uvicorn auth_service.app:create_app --factory``），
也可以挂到别的 FastAPI 应用上（``parent.mount("/auth", create_app(...))``）。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from auth_service.config import Settings
from auth_service.db import init_db
from auth_service.routes.auth import router as auth_router
from auth_service.security import AuthError, LoginRateLimiter

VERSION = "1.0.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    init_db(settings.db_path)

    app = FastAPI(
        title="SmartLife Auth Service",
        version=VERSION,
        description="通用账号、会话与权限服务；不依赖具体业务项目。",
    )
    app.state.settings = settings
    app.state.login_limiter = LoginRateLimiter(
        settings.rate_limit_attempts,
        settings.rate_limit_window,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth_router)

    @app.get("/v1/health", tags=["meta"])
    def health() -> dict:
        return {
            "status": "ok",
            "service": "auth",
            "version": VERSION,
            "registration_open": settings.allow_register,
        }

    @app.exception_handler(AuthError)
    async def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        message = str(first.get("msg") or "请求参数不正确")
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_request", "message": message}},
        )

    return app
