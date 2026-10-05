"""项目统一的结构化日志和异常上下文。"""
import logging
from typing import Any, Dict, Optional


logger = logging.getLogger("smartlife")


def _safe_context(context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    safe = {}
    for key, value in (context or {}).items():
        if key.lower() in {"api_key", "authorization", "password", "token"}:
            safe[key] = "[redacted]"
        else:
            safe[key] = str(value)[:500]
    return safe


def log_exception(component: str, error: Exception, context: Optional[Dict[str, Any]] = None) -> None:
    logger.exception(
        "[%s] %s: %s context=%s",
        component,
        type(error).__name__,
        error,
        _safe_context(context),
    )


def log_warning(component: str, message: str, context: Optional[Dict[str, Any]] = None) -> None:
    logger.warning("[%s] %s context=%s", component, message, _safe_context(context))
