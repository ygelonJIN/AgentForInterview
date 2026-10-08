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


def test_explicit_keyword_queries_database_even_when_nl2sql_model_fails():
    from app.retrieval.nl2sql import NL2SQLChain

    class BrokenChain:
        def __init__(self):
            self.calls = 0

        def invoke(self, _payload):
            self.calls += 1
            raise RuntimeError("model unavailable")

    class Executor:
        def execute(self, sql, params=()):
            assert "帐篷" in params[0]
            return {
                "rows": [{"id": 13, "name": "双人防水帐篷", "price": 599}],
                "row_count": 1,
            }

    chain = NL2SQLChain.__new__(NL2SQLChain)
    chain.chain = BrokenChain()
    chain.sql_executor = Executor()

    result = chain.query("我要买帐篷，预算3000块玩5天", max_retries=1)

    assert chain.chain.calls == 0
    assert result["results"][0]["name"] == "双人防水帐篷"
    assert result["authoritative"] is True
    assert result["fallback"] is False
    assert result["keyword_count"] == 1
    assert result["query_sources"] == ["keyword_sql"]
    assert result["failure_stage"] == ""
    assert "error" not in result


def test_keyword_results_are_returned_before_slow_nl2sql_blocks_query():
    import time

    from app.retrieval.nl2sql import NL2SQLChain

    class SlowChain:
        def invoke(self, _payload):
            time.sleep(0.2)
            raise AssertionError("慢模型不应阻塞关键词权威结果")

    class Executor:
        def execute(self, sql, params=()):
            return {
                "rows": [{"id": 6, "name": "针织修身上衣", "price": 159}],
                "row_count": 1,
            }

    chain = NL2SQLChain.__new__(NL2SQLChain)
    chain.chain = SlowChain()
    chain.sql_executor = Executor()
    chain.MODEL_ENRICHMENT_TIMEOUT_SECONDS = 0.01

    started = time.perf_counter()
    result = chain.query("推荐几件适合女生的衣服", max_retries=1)
    elapsed = time.perf_counter() - started

    assert elapsed < 1
    assert result["authoritative"] is True
    assert result["results"][0]["name"] == "针织修身上衣"
    assert "error" not in result
    assert result["query_sources"] == ["keyword_sql"]


def test_typed_keyword_query_skips_nl2sql_for_ordinary_product_search():
    from app.retrieval.nl2sql import NL2SQLChain

    class UnexpectedChain:
        def __init__(self):
            self.calls = 0

        def invoke(self, _payload):
            self.calls += 1
            raise AssertionError("普通商品过滤不应调用 NL2SQL")

    class Executor:
        def execute(self, sql, params=()):
            assert "price <= ?" in sql
            assert 800 in params
            return {
                "rows": [{"id": 13, "name": "真实跑鞋", "price": 599}],
                "row_count": 1,
            }

    chain = NL2SQLChain.__new__(NL2SQLChain)
    chain.chain = UnexpectedChain()
    chain.sql_executor = Executor()

    result = chain.query("买一双800元以内的跑鞋", max_retries=0)

    assert chain.chain.calls == 0
    assert result["count"] == 1
    assert result["keyword_count"] == 1
    assert result["model_count"] == 0
    assert result["query_sources"] == ["keyword_sql"]


def test_nl2sql_is_still_required_when_no_keyword_hits():
    from app.retrieval.nl2sql import NL2SQLChain

    class BrokenChain:
        def __init__(self):
            self.calls = 0

        def invoke(self, _payload):
            self.calls += 1
            raise RuntimeError("model unavailable")

    class UnexpectedExecutor:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("没有关键词时不应执行关键词 SQL")

    chain = NL2SQLChain.__new__(NL2SQLChain)
    chain.chain = BrokenChain()
    chain.sql_executor = UnexpectedExecutor()

    result = chain.query("帮我做一套完整方案", max_retries=1)

    assert chain.chain.calls == 2
    assert result["results"] == []
    assert result["failure_stage"] == "nl2sql_model"
    assert result["fallback"] is False


def test_nl2sql_empty_result_does_not_invent_products_without_keyword():
    from app.retrieval.nl2sql import NL2SQLChain, SQLQuery

    class EmptyExactChain:
        def invoke(self, _payload):
            return SQLQuery(
                sql="SELECT * FROM products WHERE 1 = 0",
                explanation="精确条件",
                needs_rag=[],
            )

    class Executor:
        def execute(self, *_args, **_kwargs):
            return {"rows": [], "row_count": 0}

    chain = NL2SQLChain.__new__(NL2SQLChain)
    chain.chain = EmptyExactChain()
    chain.sql_executor = Executor()

    result = chain.query("帮我做一套完整方案", max_retries=1)

    assert result["results"] == []
    assert result["fallback"] is False
    assert result["failure_stage"] == "empty_result"


def test_nl2sql_sql_execution_error_is_not_reported_as_empty_authoritative_result():
    from app.retrieval.nl2sql import NL2SQLChain, SQLQuery

    class ExactChain:
        def invoke(self, _payload):
            return SQLQuery(
                sql="SELECT * FROM products",
                explanation="精确条件",
                needs_rag=[],
            )

    class BrokenExecutor:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("database locked")

    chain = NL2SQLChain.__new__(NL2SQLChain)
    chain.chain = ExactChain()
    chain.sql_executor = BrokenExecutor()

    result = chain.query("帮我做一套完整方案", max_retries=1)

    assert result["failure_stage"] == "sql_execution"
    assert "database locked" in result["error"]
    assert result["results"] == []
    assert result["authoritative"] is False
    assert result["fallback"] is False
