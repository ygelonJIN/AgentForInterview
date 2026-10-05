"""流式正文缓冲与版本替换。"""
from typing import Any, Dict, List


def _event_type_value(event_type: Any) -> str:
    return str(getattr(event_type, "value", event_type))


class StreamResponseBuffer:
    """维护流式正文，保证修订版本替换初版而不是追加到初版之后。"""

    def __init__(self):
        self._tokens: List[str] = []
        self._done_response = ""

    def append_token(self, token: str) -> None:
        if token:
            self._tokens.append(token)

    def reset(self) -> None:
        self._tokens.clear()

    def set_done(self, response: str) -> None:
        self._done_response = response or ""

    def handle_event(self, event: Dict[str, Any]) -> None:
        event_type = _event_type_value(event.get("event", ""))
        data = event.get("data") or {}
        if event_type == "token":
            self.append_token(data.get("token", ""))
        elif event_type == "response_reset":
            self.reset()
        elif event_type == "done":
            self.set_done(data.get("response", ""))

    @property
    def text(self) -> str:
        return self._done_response or "".join(self._tokens)
