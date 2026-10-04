"""
NL2SQL 测试
"""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'products.db')

def test_database_exists():
    """测试数据库存在"""
    assert os.path.exists(DB_PATH), f"数据库不存在: {DB_PATH}"

def test_database_tables():
    """测试数据库表结构"""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in c.fetchall()}
    conn.close()
    assert "products" in tables
    assert "reviews" in tables
    assert "orders" in tables
    assert "users" in tables

def test_products_count():
    """测试商品数量"""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM products")
    count = c.fetchone()[0]
    conn.close()
    assert count >= 30, f"商品数量不足: {count}"

def test_reviews_count():
    """测试评价数量"""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM reviews")
    count = c.fetchone()[0]
    conn.close()
    assert count >= 50, f"评价数量不足: {count}"

def test_sql_query_price_filter():
    """测试价格筛选查询"""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE price < 500")
    rows = c.fetchall()
    conn.close()
    assert len(rows) > 0
    assert all(row["price"] < 500 for row in rows)

def test_sql_query_category_filter():
    """测试分类筛选查询"""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE category='户外' AND waterproof=1")
    rows = c.fetchall()
    conn.close()
    assert len(rows) > 0
    assert all(row["waterproof"] == 1 for row in rows)

def test_sql_query_keyword():
    """测试关键词搜索"""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE name LIKE '%跑步%' OR description LIKE '%跑步%'")
    rows = c.fetchall()
    conn.close()
    assert len(rows) > 0

def test_required_products():
    """测试必须包含的商品"""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    required = ["跑步鞋", "登山杖", "帐篷", "睡袋", "iPhone", "小米14", "连衣裙", "瑜伽服"]
    for name in required:
        c.execute("SELECT COUNT(*) FROM products WHERE name LIKE ?", (f"%{name}%",))
        count = c.fetchone()[0]
        assert count > 0, f"缺少商品: {name}"
    conn.close()
