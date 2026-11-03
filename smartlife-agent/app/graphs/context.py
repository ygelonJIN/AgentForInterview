"""运行期上下文：LangGraph ``context_schema`` + ``Runtime.context``。

替代旧做法（把服务对象挂在编排器实例上、把队列塞进
``config["configurable"]``）。节点从 ``runtime.context`` 取服务与事件出口，
因此图本身可以只构建一次并长期复用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.graphs.events import EventSink, TurnTiming


@dataclass
class TurnContext:
    """一轮对话的依赖注入容器。"""

    # 默认 None：Agent Server / Studio 只能传 JSON 上下文，构造不出 Python
    # 服务对象；此时由 __post_init__ 取进程级默认实例，行为与显式传入一致。
    services: Any = None
    timing: TurnTiming = field(default_factory=TurnTiming)
    trace_id: str = ""
    # 供节点读取的附加参数（actor_id、planner 覆盖等）。
    extra: dict = field(default_factory=dict)
    # 手动 ainvoke 子图时，把父节点的 writer 绑进来，保证 custom 事件不断流。
    bound_writer: Any = None

    def __post_init__(self) -> None:
        if self.services is None:
            from app.services.turn_services import get_turn_services

            self.services = get_turn_services()

    def sink_for(self, runtime: Any) -> EventSink:
        """取本轮事件出口：优先绑定 writer，其次 Runtime.stream_writer。"""
        writer = self.bound_writer
        if writer is None:
            writer = getattr(runtime, "stream_writer", None)
        if writer is None:
            from langgraph.config import get_stream_writer

            writer = get_stream_writer()
        return EventSink(writer, self.timing, self.trace_id)

    def with_extra(self, **kwargs: Any) -> "TurnContext":
        return TurnContext(
            services=self.services,
            timing=self.timing,
            trace_id=self.trace_id,
            extra={**self.extra, **kwargs},
            bound_writer=self.bound_writer,
        )

    def bind_writer(self, writer: Any) -> "TurnContext":
        return TurnContext(
            services=self.services,
            timing=self.timing,
            trace_id=self.trace_id,
            extra=dict(self.extra),
            bound_writer=writer,
        )


def build_turn_context(services: Any, trace_id: Optional[str] = None) -> TurnContext:
    if trace_id is None:
        from app.observability import new_trace_id

        trace_id = new_trace_id()
    return TurnContext(services=services, trace_id=trace_id)
