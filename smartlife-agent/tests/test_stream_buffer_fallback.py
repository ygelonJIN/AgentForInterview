"""收束正文为失败桩时，流式缓冲不能被覆盖。"""
from app.stream_response import StreamResponseBuffer, looks_like_failure_stub


def test_failure_stub_is_detected():
    assert looks_like_failure_stub("【购物推荐】\n未能完成：购物分支缺失\n\n【旅行计划】\n未能完成：旅行分支缺失")
    assert not looks_like_failure_stub("## 上海七日行程\n\n第一天……")


def test_done_stub_falls_back_to_streamed_text():
    buffer = StreamResponseBuffer()
    buffer.append_token("为了生成可靠行程，请先补充：天数。")
    buffer.append_token("上海七日行程：外滩、豫园……")

    buffer.set_done("【购物推荐】\n未能完成：购物分支缺失\n\n【旅行计划】\n未能完成：旅行分支缺失")

    assert "上海七日行程" in buffer.text
    assert "分支缺失" not in buffer.text


def test_normal_done_response_wins():
    buffer = StreamResponseBuffer()
    buffer.append_token("初稿")
    buffer.set_done("最终版")
    assert buffer.text == "最终版"
