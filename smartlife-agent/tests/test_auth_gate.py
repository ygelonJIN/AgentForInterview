"""登录门禁的界面测试：启用认证但未登录时只渲染登录页。"""
from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "app" / "main.py"


def test_login_page_renders_when_auth_enabled(monkeypatch) -> None:
    monkeypatch.setenv("SMARTLIFE_AUTH_DISABLED", "0")
    monkeypatch.setenv("SMARTLIFE_AUTH_COOKIE", "0")

    app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()

    assert not app.exception
    rendered = "\n".join(str(item.value) for item in app.markdown)
    assert "登录 SmartLife" in rendered
    labels = {str(button.label) for button in app.button}
    assert "智能助手" not in labels
