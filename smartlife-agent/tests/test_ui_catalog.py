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


def test_global_ui_layout_contract_covers_requested_fixes():
    from app.ui.theme import THEME_CSS

    required_rules = [
        '[data-testid="stHeader"]',
        '[data-testid="stSidebarHeader"]',
        'section[data-testid="stSidebar"] .stButton > button',
        '.stButton > button[kind="primary"]',
        '[data-testid="stSidebarCollapseButton"] button',
        '[data-testid="stBottom"] > div',
        '[data-testid="stBottom"] [class*="e8oj7lc3"]',
        '[data-testid="stBottom"] > div:has(> [data-testid="stBottomBlockContainer"])',
        '[data-testid="stBottomBlockContainer"]',
        '[data-testid="stChatInput"]',
        '.ui-card.directory-card',
        'min-height: 224px',
        'max-height: 224px',
        '.form-action-spacer',
        '[data-testid="stMainBlockContainer"]',
        '[data-testid="stSidebarUserContent"]',
        'padding-bottom: 9.5rem !important',
        '--control-radius: 14px',
        'border-radius: var(--control-radius) !important',
        '.sidebar-brand',
        '.execution-log-panel',
        '.execution-log-row',
        '.execution-log-message',
        '[data-testid="stChatInput"] > div > div',
        'background: var(--surface) !important',
        'background: #6366f1 !important',
        'background: #4f46e5 !important',
        'background: #ffffff !important',
        'section[data-testid="stSidebar"] [data-testid="stAlert"]',
        'color: #ffffff !important',
        'transform: translateY(-1px) !important',
        'position: absolute !important',
        'right: 13px !important',
    ]
    for rule in required_rules:
        assert rule in THEME_CSS, f"missing UI rule: {rule}"
    assert THEME_CSS.count('padding-top: var(--top-offset) !important') >= 2
    assert '--top-offset: 1.75rem' in THEME_CSS
    assert '.sidebar-brand {\n  padding: 0 2.85rem 1.15rem 0.25rem;\n}' in THEME_CSS
    assert THEME_CSS.count('border-radius: var(--control-radius) !important') >= 2
    assert ':where([data-testid="stChatInput"]) * {\n  margin: 0 !important;\n  border: 0 !important;\n  outline: 0 !important;\n  border-radius: 0 !important;' in THEME_CSS
    assert THEME_CSS.count('border: 0 !important') >= 2
    assert 'inset 3px 0 0' not in THEME_CSS
    assert 'border-left' not in THEME_CSS
    assert 'section[data-testid="stSidebar"] [data-testid="stAlert"] *' in THEME_CSS
    assert 'background: rgba(30, 27, 75, 0.28) !important' in THEME_CSS
    assert '0 0 0 1px' not in THEME_CSS
    assert '[data-testid="stBottom"] > div,\n[data-testid="stBottom"] [class*="e8oj7lc3"]' in THEME_CSS
    assert 'background-color: transparent !important' in THEME_CSS
    assert 'box-shadow: 0 18px 60px rgba(31, 42, 68, 0.08) !important' in THEME_CSS
    assert 'background: #e9edff;' in THEME_CSS
    assert 'html [data-testid="stBottomBlockContainer"] [data-testid="stChatInput"] > div' in THEME_CSS

    main_source = (
        Path(__file__).parents[1] / "app" / "main.py"
    ).read_text(encoding="utf-8")
    assert 'avatar="S"' not in main_source
    assert 'st.radio(' not in main_source
    assert 'st.select_slider(' not in main_source
    assert 'st.checkbox(' not in main_source



def test_whole_card_selection_switches_assistant_and_catalog_views():
    app = AppTest.from_file(Path(__file__).parents[1] / "app" / "main.py", default_timeout=20).run()
    assert not app.exception

    negotiation_button = next(
        button for button in app.main.button if button.label == "多人协商"
    )
    negotiation_button.click().run()
    assert not app.exception
    assert any(item.label == "参与者 ID" for item in app.text_input)

    nav_labels = {button.label for button in app.sidebar.button}
    assert nav_labels == {"智能助手", "生活目录", "记忆中心", "数据导入", "个人中心"}

    catalog_button = next(
        button for button in app.sidebar.button if button.label == "生活目录"
    )
    catalog_button.click().run()
    review_button = next(
        button for button in app.main.button if button.label == "商品评价"
    )
    review_button.click().run()
    assert not app.exception
    assert any(item.label == "搜索评价" for item in app.text_input)


def test_data_import_page_shows_self_service_templates():
    app = AppTest.from_file(Path(__file__).parents[1] / "app" / "main.py", default_timeout=20).run()
    next(button for button in app.sidebar.button if button.label == "数据导入").click().run()

    assert not app.exception
    labels = {button.label for button in app.button}
    assert {"开始数据库导入", "开始 RAG 导入"} <= labels
    assert any(item.label == "导入记录" for item in app.tabs)
    download_labels = {button.label for button in app.download_button}
    assert "数据库-products.csv" in download_labels
    assert "RAG-reviews.jsonl" in download_labels
    assert "数据库建表 SQL（仅用于空库）" in download_labels
