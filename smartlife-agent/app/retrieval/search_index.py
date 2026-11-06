"""商品检索索引：2-gram 倒排，避免 ``LIKE '%词%'`` 全表扫描。

为什么不直接用 FTS5 的 trigram 分词：trigram 只索引 3 字及以上的片段，
"帐篷""睡袋""T恤"这种两字词查不到；而 unicode61 不切中文，整段商品名会变成一个
token，同样查不到。装 jieba 又要多一个依赖。

所以这里自己做 **2-gram**：建索引时把中文按相邻两字切开（``双人帐篷`` →
``双人 人帐 帐篷``），查询时用同样的切法，词与词之间是隐式 AND。这样：

- 两字以上的词都能命中，和 ``LIKE '%词%'`` 语义一致；
- 两个字的词（帐篷/睡袋）也能走索引；
- 不加依赖，SQLite 自带 FTS5 就够；
- 单字词（"鞋"）退化回 LIKE——这种查询本来也不该用来筛商品。

规模：索引查询是 B 树查找，10 万/100 万行都是毫秒级；``LIKE`` 是线性扫描。
40 万行以后 FTS 表本身要占几百 MB，那时该换 ES/向量库，不是这里能解决的。
"""
from __future__ import annotations

import re
import sqlite3
import threading
from typing import Iterable, List, Optional, Sequence

_FTS_TABLE = "products_fts"
_INDEX_COLUMNS = ("name", "description", "subcategory", "brand")

_CJK_RUN = re.compile(r"[\u4e00-\u9fa5]+")
_WORD = re.compile(r"[A-Za-z0-9]+")

# 每个进程只尝试建一次，避免每次查询都去探测表结构
_state: dict = {"built": set()}
_lock = threading.RLock()

# 超过这个行数就要求显式跑脚本建索引，不在请求路径里做全量构建
AUTO_BUILD_MAX_ROWS = 50_000


def bigrams(text: str) -> List[str]:
    """中文按相邻两字切，英文数字按整词小写。"""
    tokens: List[str] = []
    for run in _CJK_RUN.findall(str(text or "")):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    tokens.extend(word.lower() for word in _WORD.findall(str(text or "")))
    return tokens


def _match_expression(term: str) -> Optional[str]:
    """把一个检索词翻成 FTS5 的 MATCH 表达式；单字词返回 None（走 LIKE）。"""
    tokens = bigrams(term)
    if not tokens:
        return None
    if len(term.strip()) < 2 and all(len(token) == 1 for token in tokens):
        # 单字词没有 2-gram 可用，索引里不会出现
        return None
    quoted = " ".join(f'"{token}"' for token in dict.fromkeys(tokens))
    return quoted


def _has_table(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_FTS_TABLE,)
    ).fetchone()
    return bool(row)


def _product_count(connection: sqlite3.Connection) -> int:
    try:
        return int(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0])
    except sqlite3.DatabaseError:
        return 0


def _index_count(connection: sqlite3.Connection) -> int:
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {_FTS_TABLE}").fetchone()[0])
    except sqlite3.DatabaseError:
        return -1


def build(connection: sqlite3.Connection) -> int:
    """重建索引；返回写入的条数。FTS5 不可用时抛 sqlite3.OperationalError。"""
    connection.execute(f"DROP TABLE IF EXISTS {_FTS_TABLE}")
    connection.execute(
        f"CREATE VIRTUAL TABLE {_FTS_TABLE} USING fts5(doc, tokenize='unicode61')"
    )
    rows = connection.execute(
        "SELECT id, name, description, subcategory, brand FROM products"
    ).fetchall()
    payload = []
    for row in rows:
        identifier = row[0]
        text = " ".join(str(row[index] or "") for index in range(1, len(_INDEX_COLUMNS) + 1))
        payload.append((identifier, " ".join(bigrams(text))))
    connection.executemany(
        f"INSERT INTO {_FTS_TABLE}(rowid, doc) VALUES (?, ?)", payload
    )
    connection.commit()
    return len(payload)


def ensure(connection: sqlite3.Connection, db_path: str, *, auto: bool = True) -> bool:
    """确保索引可用：缺表或行数不一致时按需重建。返回是否可用。"""
    key = str(db_path)
    with _lock:
        if key in _state["built"]:
            return True
        try:
            if not _has_table(connection):
                if not auto or _product_count(connection) > AUTO_BUILD_MAX_ROWS:
                    return False
                build(connection)
            elif _index_count(connection) != _product_count(connection):
                if not auto or _product_count(connection) > AUTO_BUILD_MAX_ROWS:
                    return False
                build(connection)
        except sqlite3.OperationalError:
            # 老 SQLite 没编译 FTS5：退回 LIKE，不阻断检索
            return False
        _state["built"].add(key)
        return True


def reset_cache(db_path: Optional[str] = None) -> None:
    with _lock:
        if db_path is None:
            _state["built"].clear()
        else:
            _state["built"].discard(str(db_path))


def match_expression(terms: Sequence[str]) -> Optional[str]:
    """把多个检索词合成一个 FTS5 MATCH 表达式。

    多词之间是隐式 AND（和原来 ``LIKE`` 逐个 AND 的语义一致）。
    只要有一个词是单字（没有 2-gram 可用），整体返回 None，调用方退回 LIKE。
    """
    tokens: List[str] = []
    for term in terms:
        if _match_expression(term) is None:
            return None
        tokens.extend(bigrams(term))
    if not tokens:
        return None
    return " ".join(f'"{token}"' for token in dict.fromkeys(tokens))
