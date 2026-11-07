from pathlib import Path

from streamlit.testing.v1 import AppTest

from app.ui.content import (
    filter_travel_resources,
    load_product_reviews,
    load_product_taxonomy,
    load_travel_resources,
)


def test_catalog_data_supports_product_and_review_views():
    taxonomy = load_product_taxonomy()
    assert {"服装", "户外", "电子"} <= set(taxonomy)
    assert all(taxonomy.values())

    reviews = load_product_reviews(min_rating=4, limit=20)
    assert reviews
    assert {"product_name", "content", "rating"} <= set(reviews[0])


def test_travel_catalog_exposes_guides_and_activities():
    resources = load_travel_resources()
    types = {item["type"] for item in resources}
    assert "目的地攻略" in types
    assert len(types) >= 4

    filtered = filter_travel_resources(resources, resource_type="目的地攻略")
    assert filtered
    assert all(item["type"] == "目的地攻略" for item in filtered)


def test_all_primary_pages_load_without_exceptions():
    app = AppTest.from_file(Path(__file__).parents[1] / "app" / "main.py", default_timeout=20).run()
    assert not app.exception

    for label in ["生活目录", "记忆中心", "个人中心", "智能助手"]:
        nav_button = next(
            button for button in app.sidebar.button
            if button.label.endswith(label)
        )
        nav_button.click().run()
        assert not app.exception, f"{label} page failed: {[e.value for e in app.exception]}"


def test_theme_uses_only_registered_design_tokens():
    """V2 改版后的样式契约：CSS 只允许消费 token，不允许出现字面色值。"""
    import re

    from app.ui.theme import THEME_CSS, _css
    from app.ui.tokens import V2_TOKENS

    css = _css()
    body = re.sub(r":root \{[^}]*\}", "", css)  # token 定义块允许出现字面值
    used = set(re.findall(r"var\(--([a-z0-9-]+)\)", css))
    assert used, "样式表必须消费 token"
    missing = used - set(V2_TOKENS)
    assert not missing, f"未登记的 token: {sorted(missing)}"
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", body), "组件样式里不能写死颜色"

    for selector in (
        '[data-testid="stMainBlockContainer"]',
        'section[data-testid="stSidebar"] .stButton > button',
        'button[kind="primary"]',
        '[data-testid="stBottom"] > div',
        '[data-testid="stChatInput"]',
        '.sl-panel',
        '.sl-row',
        '.sl-day',
        '.sl-gear',
        '.sl-budget',
        '.sl-lead',
        '.sl-rail-block',
        '.st-key-nav_menu',
        '.st-key-quick_chips',
        '.execution-log-panel',
    ):
        assert selector in css, f"缺少样式：{selector}"
    assert THEME_CSS


def test_plan_panels_render_five_sections_and_escape_html():
    from app.ui.theme import plan_panels_html

    payload = {
        "facts": {"destination": "<b>莫干山</b>", "days": 2, "people": 2, "budget": 1500},
        "constraints": [
            {"text": "目的地：<b>莫干山</b>", "source": "用户原话", "hard": True, "status": "ok"},
        ],
        "itinerary": {"days": [{"day": 1, "items": [
            {"time": "08:30", "activity": "出发", "duration_min": 60, "cost": 100},
        ]}]},
        "gear": [
            {"name": "帐篷", "qty": 1, "reason": "已有", "status": "owned"},
            {"name": "睡袋", "qty": 1, "reason": "缺口", "status": "need_buy"},
        ],
        "products": {"睡袋": {"brand": "牧高笛", "price": 199}},
        "issues": [],
    }
    html = plan_panels_html(payload)
    for index in ("01", "02", "03", "04", "05"):
        assert f'<span class="sl-panel-idx">{index}</span>' in html
    assert "<b>莫干山</b>" not in html
    assert "&lt;b&gt;莫干山" in html
    assert "未发现约束冲突" in html
    # 约束清单只留内容与来源：硬度和状态是内部口径，不再占两列。
    assert "硬度" not in html
    assert ">硬<" not in html and ">软<" not in html
    # 装备按分组横排，不再有固定三列的空列。
    assert '<div class="sl-gear-line">' in html
    assert "sl-gear-col" not in html
    # 没给人数和日期时，末尾要把默认值说出来。
    assert "默认值" in html
