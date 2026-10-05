"""NL2SQL 只读执行和 SQL 安全校验测试。"""

from pathlib import Path

import pytest

from app.retrieval.nl2sql import NL2SQLChain
from app.retrieval.sql_safety import SafeSQLExecutor, SQLSafetyError, validate_select_sql


DB_PATH = Path(__file__).parents[1] / "data" / "products.db"


def test_safe_executor_allows_select_with_bound_parameters():
    executor = SafeSQLExecutor(DB_PATH, max_rows=2, timeout_seconds=1.0)

    result = executor.execute(
        "SELECT name, price FROM products WHERE price <= ? ORDER BY price LIMIT 5",
        (500,),
    )

    assert result["row_count"] <= 2
    assert result["truncated"] is True
    assert all(row["price"] <= 500 for row in result["rows"])


@pytest.mark.parametrize("sql", [
    "DELETE FROM products",
    "UPDATE products SET price = 0",
    "PRAGMA table_info(products)",
    "ATTACH DATABASE '/tmp/evil.db' AS evil",
    "SELECT * FROM products; DELETE FROM products",
    "WITH x AS (SELECT * FROM products) DELETE FROM products",
])
def test_validate_select_sql_rejects_dangerous_queries(sql):
    with pytest.raises(SQLSafetyError):
        validate_select_sql(sql)


def test_safe_executor_denies_non_allowlisted_tables_columns_and_functions():
    executor = SafeSQLExecutor(DB_PATH, max_rows=10, timeout_seconds=1.0)

    with pytest.raises(Exception):
        executor.execute("SELECT * FROM sqlite_master")
    with pytest.raises(Exception):
        executor.execute("SELECT secret FROM products")
    with pytest.raises(Exception):
        executor.execute("SELECT load_extension('evil')")


def test_fuzzy_query_uses_parameters_instead_of_interpolating_values():
    chain = NL2SQLChain.__new__(NL2SQLChain)

    sql, params = chain._build_fuzzy_query("跑步鞋 预算300")

    assert "LIKE ?" in sql
    assert "price <= ?" in sql
    assert "%跑步鞋%" in params
    assert 300 in params
    assert "跑步鞋" not in sql
