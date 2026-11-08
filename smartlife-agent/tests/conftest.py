"""测试间清理共享 Provider 缓存和熔断状态。"""

import os
import tempfile

import pytest

# 必须在任何 app 模块被导入前设好：AppTest 跑 app/main.py 时会把 session_state
# 里的消息迁移进会话库，不换库就会把测试夹具写进真实聊天记录。
os.environ["SMARTLIFE_CONVERSATIONS_DB"] = os.path.join(
    tempfile.mkdtemp(prefix="smartlife-test-db-"),
    "conversations.db",
)

from app.tools.providers import reset_provider_runtime_state


@pytest.fixture(autouse=True)
def _reset_provider_runtime_state():
    reset_provider_runtime_state()
    yield
    reset_provider_runtime_state()
