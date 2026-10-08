"""测试间清理共享 Provider 缓存和熔断状态。"""

import pytest

from app.tools.providers import reset_provider_runtime_state


@pytest.fixture(autouse=True)
def _reset_provider_runtime_state():
    reset_provider_runtime_state()
    yield
    reset_provider_runtime_state()
