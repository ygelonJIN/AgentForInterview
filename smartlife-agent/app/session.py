"""会话身份和短期历史线程键。"""
from urllib.parse import quote


def build_thread_id(user_id: str, scene: str, session_id: str) -> str:
    """构造隔离的 thread_id，避免不同用户、场景和浏览器会话共享历史。"""
    values = {"user_id": user_id, "scene": scene, "session_id": session_id}
    for name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 不能为空")
    encoded = {name: quote(value.strip(), safe="") for name, value in values.items()}
    return f"{encoded['user_id']}:{encoded['scene']}:{encoded['session_id']}"
