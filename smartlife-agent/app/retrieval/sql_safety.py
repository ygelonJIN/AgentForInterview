"""SQLite 只读执行和静态 SQL 安全校验。"""
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple


class SQLSafetyError(ValueError):
    """SQL 未通过安全校验。"""


_ALLOWED_TABLES = {
    "products": {
        "id", "name", "category", "subcategory", "price", "waterproof",
        "brand", "stock", "description", "rating", "created_at",
    },
    "reviews": {
        "id", "product_id", "user_id", "content", "rating", "created_at",
    },
    "orders": {
        "id", "user_id", "product_id", "quantity", "status", "total_price", "created_at",
    },
    "users": {
        "id", "name", "age", "preferences", "budget", "created_at",
    },
}
_DENIED_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER",
    "PRAGMA", "ATTACH", "DETACH", "VACUUM", "REINDEX", "ANALYZE",
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "LOAD_EXTENSION",
    "GRANT", "REVOKE",
}
_DENIED_FUNCTIONS = {"load_extension", "writefile", "readfile", "edit"}
_ALLOWED_AUTHORIZER_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def validate_select_sql(sql: str) -> str:
    """校验 SQL 只读、单语句、只访问允许的 SELECT 查询。"""
    if not isinstance(sql, str) or not sql.strip():
        raise SQLSafetyError("SQL 不能为空")
    if len(sql) > 10000:
        raise SQLSafetyError("SQL 过长")

    candidate = sql.strip()
    if candidate.endswith(";"):
        candidate = candidate[:-1].strip()
    if not candidate:
        raise SQLSafetyError("SQL 不能为空")
    if ";" in candidate:
        raise SQLSafetyError("禁止一次执行多条 SQL 语句")
    if not re.match(r"^(SELECT|WITH)\b", candidate, re.IGNORECASE):
        raise SQLSafetyError("只允许 SELECT 查询")

    keyword_match = re.search(
        r"\b(" + "|".join(sorted(_DENIED_KEYWORDS)) + r")\b",
        candidate,
        re.IGNORECASE,
    )
    if keyword_match:
        raise SQLSafetyError(f"禁止 SQL 关键字: {keyword_match.group(1).upper()}")
    if not sqlite3.complete_statement(candidate + ";"):
        raise SQLSafetyError("SQL 语法不完整")
    return candidate


class SafeSQLExecutor:
    """通过 SQLite authorizer、只读连接、超时和行数上限执行查询。"""

    def __init__(
        self,
        db_path: str,
        max_rows: int = 200,
        timeout_seconds: float = 2.0,
    ):
        if max_rows <= 0:
            raise ValueError("max_rows 必须大于 0")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self.db_path = str(Path(db_path))
        self.max_rows = max_rows
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _authorizer(action: int, arg1: str, arg2: str, _db_name: str, _source: str) -> int:
        if action not in _ALLOWED_AUTHORIZER_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ:
            table = (arg1 or "").lower()
            column = (arg2 or "").lower()
            if table not in _ALLOWED_TABLES:
                return sqlite3.SQLITE_DENY
            if column != "*" and column not in _ALLOWED_TABLES[table]:
                return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            function_name = (arg2 or arg1 or "").lower()
            if function_name in _DENIED_FUNCTIONS:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def execute(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> Dict[str, Any]:
        validated = validate_select_sql(sql)
        uri = f"file:{self.db_path}?mode=ro"
        started = time.monotonic()
        connection = sqlite3.connect(uri, uri=True, timeout=self.timeout_seconds)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.set_authorizer(self._authorizer)

            def _progress_handler():
                return 1 if time.monotonic() - started >= self.timeout_seconds else 0

            connection.set_progress_handler(_progress_handler, 1000)
            cursor = connection.cursor()
            cursor.execute(validated, tuple(params or ()))
            rows = cursor.fetchmany(self.max_rows + 1)
            truncated = len(rows) > self.max_rows
            rows = rows[:self.max_rows]
            return {
                "rows": [dict(row) for row in rows],
                "row_count": len(rows),
                "truncated": truncated,
                "sql": validated,
            }
        finally:
            connection.close()
