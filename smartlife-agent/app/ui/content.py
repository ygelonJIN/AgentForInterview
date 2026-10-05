"""商品、评价与旅行目录的数据读取层。"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


APP_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = APP_ROOT / "data"
DEFAULT_DB_PATH = DATA_ROOT / "products.db"


def get_database_path() -> Path:
    return DEFAULT_DB_PATH


def _dict_rows(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]


def load_product_summary(db_path: Path = DEFAULT_DB_PATH) -> Dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        reviews = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        categories = conn.execute("SELECT COUNT(DISTINCT category) FROM products").fetchone()[0]
        avg_rating = conn.execute("SELECT COALESCE(AVG(rating), 0) FROM products").fetchone()[0]
    return {
        "products": products,
        "reviews": reviews,
        "categories": categories,
        "average_rating": round(float(avg_rating), 1),
    }


def load_product_taxonomy(db_path: Path = DEFAULT_DB_PATH) -> Dict[str, List[str]]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT category, subcategory, COUNT(*) AS product_count
            FROM products
            GROUP BY category, subcategory
            ORDER BY category, product_count DESC, subcategory
            """
        ).fetchall()
    taxonomy: Dict[str, List[str]] = {}
    for row in rows:
        taxonomy.setdefault(row[0], []).append(row[1] or "其他")
    return taxonomy


def load_products(
    *,
    category: str = "全部",
    subcategory: str = "全部",
    search: str = "",
    max_price: Optional[float] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if category != "全部":
        clauses.append("category = ?")
        params.append(category)
    if subcategory != "全部":
        clauses.append("COALESCE(subcategory, '其他') = ?")
        params.append(subcategory)
    if search.strip():
        clauses.append("(name LIKE ? OR brand LIKE ? OR description LIKE ?)")
        term = f"%{search.strip()}%"
        params.extend([term, term, term])
    if max_price is not None:
        clauses.append("price <= ?")
        params.append(max_price)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT p.*,
                   COUNT(r.id) AS review_count,
                   COALESCE(AVG(r.rating), p.rating) AS community_rating
            FROM products p
            LEFT JOIN reviews r ON r.product_id = p.id
            {where}
            GROUP BY p.id
            ORDER BY p.rating DESC, p.price ASC
            """,
            params,
        ).fetchall()
    return _dict_rows(rows)


def load_product(product_id: int, db_path: Path = DEFAULT_DB_PATH) -> Optional[Dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT p.*, COUNT(r.id) AS review_count,
                   COALESCE(AVG(r.rating), p.rating) AS community_rating
            FROM products p
            LEFT JOIN reviews r ON r.product_id = p.id
            WHERE p.id = ?
            GROUP BY p.id
            """,
            (product_id,),
        ).fetchone()
    return dict(row) if row else None


def load_product_reviews(
    *,
    product_id: Optional[int] = None,
    category: str = "全部",
    min_rating: int = 1,
    search: str = "",
    limit: int = 100,
    db_path: Path = DEFAULT_DB_PATH,
) -> List[Dict[str, Any]]:
    clauses = ["r.rating >= ?"]
    params: List[Any] = [min_rating]
    if product_id is not None:
        clauses.append("r.product_id = ?")
        params.append(product_id)
    if category != "全部":
        clauses.append("p.category = ?")
        params.append(category)
    if search.strip():
        clauses.append("(p.name LIKE ? OR r.content LIKE ? OR r.user_id LIKE ?)")
        term = f"%{search.strip()}%"
        params.extend([term, term, term])
    params.append(limit)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT r.*, p.name AS product_name, p.category, p.subcategory, p.brand
            FROM reviews r
            JOIN products p ON p.id = r.product_id
            WHERE {' AND '.join(clauses)}
            ORDER BY r.rating DESC, r.created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return _dict_rows(rows)


def load_orders(user_id: str, db_path: Path = DEFAULT_DB_PATH) -> List[Dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT o.*, p.name AS product_name, p.brand, p.category
            FROM orders o
            JOIN products p ON p.id = o.product_id
            WHERE o.user_id = ?
            ORDER BY o.created_at DESC
            """,
            (user_id,),
        ).fetchall()
    return _dict_rows(rows)


def load_user(user_id: str, db_path: Path = DEFAULT_DB_PATH) -> Optional[Dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def _slug_title(path: Path) -> str:
    return path.stem.replace("_", " ").title()


def _summary(body: str, limit: int = 92) -> str:
    text = re.sub(r"【[^】]+】", "", body)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def load_travel_resources(root: Path = DATA_ROOT) -> List[Dict[str, Any]]:
    resources: List[Dict[str, Any]] = []
    guides_root = root / "guides"
    for path in sorted(guides_root.glob("*.txt")):
        body = path.read_text(encoding="utf-8").strip()
        first_line = body.splitlines()[0].strip() if body else _slug_title(path)
        resources.append({
            "id": f"guide-{path.stem}",
            "type": "目的地攻略",
            "title": first_line,
            "description": _summary(body),
            "content": body,
            "source": path.name,
            "tags": ("深度阅读", "实用指南"),
        })

    activities_root = root / "activities"
    for path in sorted(activities_root.glob("*.txt")):
        body = path.read_text(encoding="utf-8").strip()
        current_type = "活动灵感"
        title = ""
        lines: List[str] = []
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            heading = re.match(r"^【(.+)】$", line)
            if heading:
                current_type = heading.group(1)
                continue
            item = re.match(r"^\d+\.\s+(.*)$", line)
            if item:
                if title and lines:
                    resources.append({
                        "id": f"activity-{path.stem}-{len(resources)}",
                        "type": current_type,
                        "title": title,
                        "description": _summary(" ".join(lines)),
                        "content": "；".join(lines),
                        "source": path.name,
                        "tags": (current_type, _slug_title(path)),
                    })
                title = item.group(1).split(" - ")[0]
                lines = [item.group(1)]
            elif title and line.startswith("- "):
                lines.append(line[2:])
        if title and lines:
            resources.append({
                "id": f"activity-{path.stem}-{len(resources)}",
                "type": current_type,
                "title": title,
                "description": _summary(" ".join(lines)),
                "content": "；".join(lines),
                "source": path.name,
                "tags": (current_type, _slug_title(path)),
            })
    return resources


def filter_travel_resources(
    resources: Iterable[Dict[str, Any]],
    *,
    resource_type: str = "全部",
    search: str = "",
) -> List[Dict[str, Any]]:
    term = search.strip().lower()
    output = []
    for resource in resources:
        if resource_type != "全部" and resource.get("type") != resource_type:
            continue
        haystack = " ".join([
            str(resource.get("title", "")),
            str(resource.get("description", "")),
            str(resource.get("content", "")),
            " ".join(resource.get("tags", ())),
        ]).lower()
        if term and term not in haystack:
            continue
        output.append(resource)
    return output
