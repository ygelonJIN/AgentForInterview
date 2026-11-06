"""商品检索索引：2-gram 倒排，结果必须与 LIKE 一致。"""

import sqlite3

import pytest

from app.retrieval import search_index
from app.retrieval.repositories import ProductRepository

_COLUMNS = (
    "id INTEGER PRIMARY KEY, name TEXT, category TEXT, subcategory TEXT, price REAL, "
    "waterproof INTEGER, brand TEXT, stock INTEGER, description TEXT, rating REAL, "
    "created_at TEXT, specs TEXT, lead_time_days INTEGER"
)

_ROWS = [
    ("黑鹿 云尚 双人帐篷 卡其", "户外", "露营", 599, "黑鹿", "防雨外帐，双人使用"),
    ("挪客 暖冬 5℃棉睡袋 炭黑", "户外", "露营", 191, "挪客", "露营保暖"),
    ("探路者 山脊 中端防水登山鞋 轻量款", "户外", "登山", 699, "探路者", "防水透气"),
    ("优衣库 极地 厚羽绒服", "服装", "服装", 799, "优衣库", "冬季保暖"),
    ("纯棉圆领T恤", "服装", "男装", 129, "无印", "夏季短袖"),
    ("iPhone 15 Pro Max", "电子", "手机", 9999, "Apple", "旗舰手机"),
]


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE products ({_COLUMNS})")
    conn.executemany(
        "INSERT INTO products (name, category, subcategory, price, brand, description, "
        "rating, stock) VALUES (?,?,?,?,?,?,4.5,5)",
        _ROWS,
    )
    conn.commit()
    conn.close()
    return path


def test_bigram_indexes_two_char_chinese_words():
    assert "帐篷" in search_index.bigrams("双人帐篷")
    assert search_index.bigrams("iPhone 15") == ["iphone", "15"]


def test_single_char_query_has_no_bigram():
    assert search_index.match_expression(["鞋"]) is None
    assert search_index.match_expression(["帐篷"]) is not None


def test_search_results_match_like_semantics(tmp_path):
    db = _make_db(tmp_path / "products.db")
    search_index.reset_cache(str(db))
    repo = ProductRepository(str(db))

    for query in ("帐篷", "睡袋", "羽绒服", "T恤", "iPhone", "登山鞋"):
        indexed = [row["id"] for row in repo.search(query=query, limit=10)]

        plain = ProductRepository(str(db))
        # 禁用索引后走 LIKE，作为对照
        original = search_index.ensure
        search_index.ensure = lambda *args, **kwargs: False
        try:
            expected = [row["id"] for row in plain.search(query=query, limit=10)]
        finally:
            search_index.ensure = original

        assert indexed == expected, query


def test_multi_term_query_is_and(tmp_path):
    db = _make_db(tmp_path / "products.db")
    search_index.reset_cache(str(db))
    repo = ProductRepository(str(db))

    both = repo.search(query="防水 登山鞋", limit=10)
    assert [row["name"] for row in both] == ["探路者 山脊 中端防水登山鞋 轻量款"]

    assert repo.search(query="防水 帐篷", limit=10) == []


def test_index_is_rebuilt_when_catalog_changes(tmp_path):
    db = _make_db(tmp_path / "products.db")
    search_index.reset_cache(str(db))
    repo = ProductRepository(str(db))
    assert repo.search(query="帐篷", limit=5)

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO products (name, category, subcategory, price, brand, description, "
        "rating, stock) VALUES ('牧高笛 轻量 单帐 军绿','户外','露营',399,'牧高笛','单人帐篷',4.2,5)"
    )
    conn.commit()
    conn.close()
    search_index.reset_cache(str(db))

    rows = repo.search(query="帐篷", limit=10)
    assert len(rows) == 2


def test_falls_back_to_like_without_index(tmp_path):
    db = _make_db(tmp_path / "products.db")
    search_index.reset_cache(str(db))

    with sqlite3.connect(db) as conn:
        # 模拟"没有 FTS5"：让建索引直接失败
        conn.execute("CREATE TABLE products_fts_broken (x)")
    search_index.AUTO_BUILD_MAX_ROWS = 0
    try:
        repo = ProductRepository(str(db))
        rows = repo.search(query="帐篷", limit=5)
        assert rows and "帐篷" in rows[0]["name"]
    finally:
        search_index.AUTO_BUILD_MAX_ROWS = 50_000


@pytest.mark.parametrize("query", ["", "  "])
def test_empty_query_returns_all_rows(query, tmp_path):
    db = _make_db(tmp_path / "products.db")
    search_index.reset_cache(str(db))
    repo = ProductRepository(str(db))

    assert len(repo.search(query=query, limit=10)) == len(_ROWS)
