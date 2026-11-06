"""商品库目录摘要：给模型看的是品类，不是全量商品。"""

import sqlite3

from app.trip_gear import catalog


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, category TEXT, "
        "subcategory TEXT, price REAL, rating REAL, stock INTEGER)"
    )
    rows = [
        ("挪客 云尚 双人帐篷 卡其", "户外", "露营", 599, 4.7, 5),
        ("挪客 暖冬 -25℃羽绒睡袋 炭黑", "户外", "露营", 899, 4.8, 5),
        ("迪卡侬 星火 基础头灯", "户外", "露营", 59, 4.5, 5),
        ("探路者 山脊 中端防水登山鞋 轻量款", "户外", "登山", 699, 4.6, 5),
        ("纯棉圆领T恤", "服装", "男装", 129, 4.4, 5),
        ("iPhone 15 Pro Max", "电子", "手机", 9999, 4.9, 5),
    ]
    conn.executemany(
        "INSERT INTO products (name, category, subcategory, price, rating, stock) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_digest_lists_categories_and_type_words(tmp_path):
    db = tmp_path / "products.db"
    _make_db(db)
    catalog.clear_cache()

    digest = catalog.catalog_digest(str(db))

    assert "户外/露营" in digest
    assert "帐篷" in digest and "睡袋" in digest   # 品类词要露出来
    assert "登山鞋" in digest
    assert "iPhone 15 Pro Max" in digest          # 抽不出中文品类词时退回完整名
    assert "以上是全部品类（4 个）" in digest


def test_digest_is_bounded_by_categories_not_skus(tmp_path):
    db = tmp_path / "products.db"
    _make_db(db)
    catalog.clear_cache()

    digest = catalog.catalog_digest(str(db))

    assert len(digest.splitlines()) <= 10


def test_missing_database_returns_empty_digest(tmp_path):
    catalog.clear_cache()
    assert catalog.catalog_digest(str(tmp_path / "nope.db")) == ""


def test_digest_is_cached_and_can_be_cleared(tmp_path):
    db = tmp_path / "products.db"
    _make_db(db)
    catalog.clear_cache()

    assert catalog.catalog_digest(str(db)) == catalog.catalog_digest(str(db))
    catalog.clear_cache()
    assert catalog.catalog_digest(str(db)) != ""
