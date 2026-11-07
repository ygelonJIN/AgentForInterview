"""把提示词 + 大模型变成"稳定拿到结构化对象"的统一入口。

为什么不直接用 ``llm.with_structured_output(Model)``：
它走 OpenAI 的 ``response_format: json_schema``，而很多 OpenAI 兼容服务不支持
这个参数，会直接返回 ``400 This response_format type is unavailable now``。
症状是"模型永远失败、逻辑永远走兜底"，很难发现。

这里的做法是：提示词里明确要求 JSON → 本地解析（容错代码块、前后解释、尾逗号）
→ Pydantic 校验 → 失败时把错误回灌重试一次。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Type, TypeVar

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from app.observability import extract_model_usage, timed_span

TModel = TypeVar("TModel")

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


class JsonExtractionError(ValueError):
    """模型输出里取不出合法 JSON，或结构与模型不匹配。"""


def message_text(response: Any) -> str:
    """把模型返回抽成纯文本，兼容 content 为字符串或分块列表。"""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content or "")


def extract_json_object(text: str) -> Dict[str, Any]:
    """从可能带说明文字或代码块的输出里取出 JSON 对象。"""
    raw = _FENCE_RE.sub("", str(text or "").strip())
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise JsonExtractionError("输出中没有 JSON 对象")
    candidate = raw[start:end + 1]
    for attempt in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
        try:
            payload = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise JsonExtractionError("JSON 解析失败")


def _render(prompt: Any, variables: Optional[Dict[str, Any]]) -> List[BaseMessage]:
    if prompt is None:
        return []
    if hasattr(prompt, "format_messages"):
        return list(prompt.format_messages(**(variables or {})))
    return list(prompt)


def _skeleton(value: Any, depth: int = 0) -> Any:
    """按 Pydantic 模型生成 JSON 骨架，用示例值占位。"""
    if depth > 4:
        return {}
    origin = getattr(value, "__origin__", None)
    if origin in (list, List, tuple, Sequence):
        args = getattr(value, "__args__", ())
        return [_skeleton(args[0], depth + 1)] if args else []
    if origin in (dict, Dict):
        return {}
    if hasattr(value, "model_fields"):
        return {
            name: _skeleton(field.annotation, depth + 1)
            for name, field in value.model_fields.items()
        }
    if value in (str, Optional[str]):
        return "文本"
    if value is bool:
        return False
    if value is int:
        return 0
    if value is float:
        return 0.0
    # Optional[X] / Union[X, None]
    args = getattr(value, "__args__", ())
    for arg in args:
        if arg is type(None):
            continue
        return _skeleton(arg, depth + 1)
    return "文本" if str(value).endswith("str'>") else {}


def schema_hint(schema: Type[Any]) -> str:
    """把模型的字段结构写进提示词。

    以前靠 ``with_structured_output`` 自动注入 schema，现在必须显式告诉模型，
    否则它会自己编字段名（例如把 instruction 写成 action），校验必然失败。
    """
    skeleton = json.dumps(_skeleton(schema), ensure_ascii=False, indent=2)
    return (
        "必须严格按以下 JSON 结构输出，字段名和层级保持一致，不要增删字段：\n"
        + skeleton
        + "\n只输出 JSON 对象本身，不要解释，不要代码块标记。"
    )


def _with_schema(messages: List[BaseMessage], schema: Type[Any], enabled: bool) -> List[BaseMessage]:
    if not enabled:
        return messages
    hint = SystemMessage(content=schema_hint(schema))
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return messages[:index] + [hint] + messages[index:]
    return messages + [hint]


def _retry_hint(error: str) -> str:
    return (
        "上一次输出无法解析为要求的 JSON，错误：" + error[:200] +
        "。请只输出一个 JSON 对象，不要解释、不要代码块标记。"
    )


def _parse(text: str, schema: Type[TModel]) -> TModel:
    return schema.model_validate(extract_json_object(text))


def invoke_json(
    llm: Any,
    prompt: Any,
    schema: Type[TModel],
    variables: Optional[Dict[str, Any]] = None,
    *,
    retries: int = 1,
    schema_hint_enabled: bool = True,
) -> TModel:
    """同步版：调用模型并返回校验后的对象。"""
    messages = _with_schema(_render(prompt, variables), schema, schema_hint_enabled)
    last_error = ""
    for attempt in range(max(0, retries) + 1):
        payload = messages if attempt == 0 else messages + [HumanMessage(content=_retry_hint(last_error))]
        # 结构化调用也是模型调用：不在这里记 usage，本轮 Token 就会漏掉整段规划。
        with timed_span("model.json") as measurement:
            response = llm.invoke(payload)
            measurement.add_attributes(extract_model_usage(response))
        try:
            return _parse(message_text(response), schema)
        except (JsonExtractionError, ValidationError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    raise JsonExtractionError(last_error or "模型输出为空")


async def ainvoke_json(
    llm: Any,
    prompt: Any,
    schema: Type[TModel],
    variables: Optional[Dict[str, Any]] = None,
    *,
    retries: int = 1,
    schema_hint_enabled: bool = True,
) -> TModel:
    """异步版：调用模型并返回校验后的对象。"""
    messages = _with_schema(_render(prompt, variables), schema, schema_hint_enabled)
    last_error = ""
    for attempt in range(max(0, retries) + 1):
        payload = messages if attempt == 0 else messages + [HumanMessage(content=_retry_hint(last_error))]
        with timed_span("model.json") as measurement:
            response = await llm.ainvoke(payload)
            measurement.add_attributes(extract_model_usage(response))
        try:
            return _parse(message_text(response), schema)
        except (JsonExtractionError, ValidationError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    raise JsonExtractionError(last_error or "模型输出为空")
