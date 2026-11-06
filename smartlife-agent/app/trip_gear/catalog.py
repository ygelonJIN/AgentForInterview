"""商品库目录摘要：给模型看的"我们卖什么"。

两个容易搞错的点：

1. **扫的是品类，不是商品。** 这里只做 ``GROUP BY category, subcategory``，
   摘要长度由品类数决定，与 SKU 总量无关——11 个品类 2000 件和 11 个品类
   十亿件，摘要一样长。真正的扩容瓶颈在商品检索（``LIKE '%词%'`` 走不了索引），
   不在这一层。

2. **示例要露出品类词，不是评分最高的三件。** 按评分取前三，露营只会露出
   头灯和防潮垫，帐篷和睡袋根本不在模型眼前；模型再懂常识也没法指着写。
   所以从商品名里抽"品类词"（第三个词位，如 ``挪客 暖冬 -25℃羽绒睡袋 炭黑``
   → ``羽绒睡袋``），去重后再给模型。

商品到几十万以后的升级路径也写在这里：品类统计改成物化表（导入时增量更新），
品类特别多时按一级品类分层下钻，检索换成 FTS5/向量索引。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

_DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "products.db",
)

_TYPES_PER_SUBCATEGORY = 8
_NAMES_PER_SUBCATEGORY = 400
_MAX_ROWS = 60
_CACHE_TTL_SECONDS = 300.0
_SUMMARY_TABLE = "catalog_summary"
_cache: Dict[str, Any] = {"key": "", "at": 0.0, "digest": "", "rows": []}
_lock = threading.RLock()

_BRACKET_RE = re.compile(r"[（）()【】\[\]]")
_LEADING_MODIFIERS = re.compile(
    r"^(?:轻量|升级|基础|高帮|低帮|加厚|专业|舒适|大容量|折叠|便携|入门|中端|高端|防水)"
)
_PURE_LATIN_OR_DIGIT = re.compile(r"^[0-9A-Za-z.+\- ]+$")
# 颜色/版本这类词不是品类，出现在名字最后一位时先跳过
_VARIANT_WORDS = {
    "炭黑", "雾蓝", "卡其", "黑色", "白色", "灰色", "藏青", "军绿", "红色",
    "蓝色", "绿色", "米白", "深灰", "浅灰", "升级款", "轻量款", "标准款",
    "入门款", "中端款", "高端款", "男款", "女款",
}


def _db_path(db_path: Optional[str] = None) -> str:
    return db_path or os.environ.get("SMARTLIFE_PRODUCTS_DB") or _DEFAULT_DB


def _type_of(name: str) -> str:
    """从商品名里抽品类词。

    ``迪卡侬 星火 基础头灯`` → ``头灯``；``挪客 暖冬 -25℃羽绒睡袋 炭黑`` → ``羽绒睡袋``。
    从后往前找第一个"像品类的中文词"，跳过颜色/版本词和纯规格（40L、Pro）。
    全都抽不出来（纯外文型号）时退回完整商品名，宁可长一点也不要给模型 "14"。
    """
    text = _BRACKET_RE.sub(" ", str(name or "")).strip()
    parts = [part for part in text.split() if part]
    for token in reversed(parts[1:] if len(parts) > 1 else parts):
        if token in _VARIANT_WORDS:
            continue
        cleaned = re.sub(r"^[^\u4e00-\u9fa5]+", "", token).strip()
        cleaned = re.sub(r"款$", "", cleaned).strip()
        if len(cleaned) < 2 or not re.search(r"[\u4e00-\u9fa5]", cleaned):
            continue
        if _PURE_LATIN_OR_DIGIT.match(cleaned):
            continue
        stripped = _LEADING_MODIFIERS.sub("", cleaned).strip()
        return stripped or cleaned
    return text or str(name or "")


def _types_for(conn: sqlite3.Connection, category: str, subcategory: str) -> List[str]:
    rows = conn.execute(
        "SELECT name FROM products WHERE category = ? AND subcategory = ? "
        "ORDER BY rating DESC, price ASC LIMIT ?",
        (category, subcategory, _NAMES_PER_SUBCATEGORY),
    ).fetchall()
    types: List[str] = []
    for (name,) in rows:
        candidate = _type_of(name)
        if candidate and candidate not in types:
            types.append(candidate)
        if len(types) >= _TYPES_PER_SUBCATEGORY:
            break
    return types


def _summary_ready(connection: sqlite3.Connection) -> bool:
    """物化表存在且行数与商品数一致才算新鲜。"""
    try:
        row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(count), 0) FROM " + _SUMMARY_TABLE
        ).fetchone()
    except sqlite3.DatabaseError:
        return False
    if not row or int(row[0]) <= 0:
        return False
    products = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    return int(row[1]) == int(products)


def _rows_from_summary(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = connection.execute(
        "SELECT category, subcategory, count, types FROM " + _SUMMARY_TABLE +
        " ORDER BY count DESC LIMIT ?",
        (_MAX_ROWS,),
    ).fetchall()
    result = []
    for category, subcategory, count, types in rows:
        try:
            parsed = json.loads(types or "[]")
        except (TypeError, ValueError):
            parsed = []
        result.append({
            "category": category,
            "subcategory": subcategory,
            "count": int(count),
            "types": [str(item) for item in parsed],
        })
    return result


def _compute_catalog(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    groups = connection.execute(
        "SELECT category, subcategory, COUNT(*) FROM products "
        "GROUP BY category, subcategory ORDER BY COUNT(*) DESC"
    ).fetchall()
    rows = []
    for category, subcategory, count in groups[:_MAX_ROWS]:
        rows.append({
            "category": category,
            "subcategory": subcategory,
            "count": int(count),
            "types": _types_for(connection, category, subcategory),
        })
    return rows


def refresh_summary(connection: sqlite3.Connection) -> int:
    """把当前品类统计写进物化表；导入商品后跑一次，读路径就不再 GROUP BY。"""
    rows = _compute_catalog(connection)
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {_SUMMARY_TABLE} ("
        "category TEXT, subcategory TEXT, count INTEGER, types TEXT, updated_at TEXT, "
        "PRIMARY KEY (category, subcategory))"
    )
    connection.execute(f"DELETE FROM {_SUMMARY_TABLE}")
    connection.executemany(
        f"INSERT INTO {_SUMMARY_TABLE}(category, subcategory, count, types, updated_at) "
        "VALUES (?,?,?,?,datetime('now'))",
        [(row["category"], row["subcategory"], row["count"], json.dumps(row["types"], ensure_ascii=False)) for row in rows],
    )
    connection.commit()
    return len(rows)


def load_catalog(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """返回 [{category, subcategory, count, types[]}]。

    优先读物化表（导入时维护），物化表缺失或与商品数不一致时现算一次并回填。
    """
    path = _db_path(db_path)
    if not os.path.exists(path):
        return []
    with sqlite3.connect(path) as conn:
        if _summary_ready(conn):
            return _rows_from_summary(conn)
        rows = _compute_catalog(conn)
        try:
            refresh_summary(conn)
        except sqlite3.DatabaseError:
            pass
        return rows


def catalog_digest(db_path: Optional[str] = None) -> str:
    """拼成一段给模型看的目录文本；库不存在或为空时返回空串（调用方降级）。"""
    path = _db_path(db_path)
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return ""

    key = f"{path}:{stamp}"
    with _lock:
        if _cache["key"] == key and time.monotonic() - _cache["at"] < _CACHE_TTL_SECONDS:
            return _cache["digest"]

    rows = load_catalog(db_path)
    if not rows:
        return ""

    lines = ["【我们商品库里现有的品类】"]
    for row in rows:
        types = "、".join(row["types"]) or "（无示例）"
        lines.append(f"- {row['category']}/{row['subcategory']}（{row['count']} 件）：{types}")
    lines.append(
        f"以上是全部品类（{len(rows)} 个）。装备清单只能出现这些品类里的东西；"
        "目录里没有的品类不要写——写了也买不到。"
    )
    digest = "\n".join(lines)

    with _lock:
        _cache.update({"key": key, "at": time.monotonic(), "digest": digest, "rows": rows})
    return digest


def catalog_rows(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """结构化目录（测试与调试用）。"""
    if not catalog_digest(db_path):
        return []
    with _lock:
        return list(_cache["rows"])


def clear_cache() -> None:
    with _lock:
        _cache.update({"key": "", "at": 0.0, "digest": "", "rows": []})
