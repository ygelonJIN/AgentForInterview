"""流式正文缓冲与版本替换。"""
from typing import Any, Dict, List


def looks_like_failure_stub(text: str) -> bool:
    """判断收束正文是不是“全部分支失败”的占位文案。"""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return False
    if "分支缺失" in (text or ""):
        return True
    if len(lines) > 4:
        return False
    return all(("未能完成" in line) or line.startswith("【") for line in lines)


def _event_type_value(event_type: Any) -> str:
    return str(getattr(event_type, "value", event_type))


class StreamResponseBuffer:
    """维护流式正文，保证修订版本替换初版而不是追加到初版之后。"""

    def __init__(self):
        self._tokens: List[str] = []
        self._section_tokens: Dict[str, List[str]] = {}
        self._done_response = ""

    def append_token(self, token: str, section: str = "") -> None:
        if token:
            if section:
                self._section_tokens.setdefault(section, []).append(token)
            else:
                self._tokens.append(token)

    def reset(self, section: str = "") -> None:
        if section:
            self._section_tokens.pop(section, None)
        else:
            self._tokens.clear()
            self._section_tokens.clear()

    def set_done(self, response: str) -> None:
        self._done_response = response or ""

    def handle_event(self, event: Dict[str, Any]) -> None:
        event_type = _event_type_value(event.get("event", ""))
        data = event.get("data") or {}
        if event_type == "token":
            self.append_token(data.get("token", ""), str(data.get("section") or ""))
        elif event_type == "response_reset":
            self.reset(str(data.get("section") or ""))
        elif event_type == "done":
            self.set_done(data.get("response", ""))

    def _buffered_text(self) -> str:
        parts: List[str] = []
        plain = "".join(self._tokens)
        if plain:
            parts.append(plain)
        for section, title in (("shopping", "购物推荐"), ("travel", "旅行计划")):
            body = "".join(self._section_tokens.get(section, []))
            if body:
                parts.append(f"【{title}】\n{body}")
        for section, tokens in self._section_tokens.items():
            if section not in {"shopping", "travel"}:
                body = "".join(tokens)
                if body:
                    parts.append(body)
        return "\n\n".join(parts)

    @property
    def text(self) -> str:
        buffered = self._buffered_text()
        if self._done_response:
            # 收束文案只剩失败桩时，不要把已经流出来的正文覆盖掉。
            if buffered and looks_like_failure_stub(self._done_response):
                return buffered
            return self._done_response
        return buffered
