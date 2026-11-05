#!/usr/bin/env python3
"""给 goods 表补规格字段：``specs`` 与 ``lead_time_days``。

为什么单独写迁移而不是重建库：``data/products.db`` 里可能已经有增量导入的
真实数据，重建会把它们冲掉。本脚本只做加法——建列、按目录回填，不动任何
既有行，并且改库前先留一份备份。

用法（在项目根目录）::

    python scripts/migrate_product_specs.py --dry-run    # 先看要改什么
    python scripts/migrate_product_specs.py              # 真正执行

重复执行是安全的：列已存在就跳过，已回填的行不会被覆盖成空。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.trip_gear.spec_match import parse_specs  # noqa: E402
from data.product_specs import lead_time_for, specs_for  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / "data" / "products.db"


def _columns(conn: sqlite3.Connection) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(products)")}


def _add_columns(conn: sqlite3.Connection, existing: set, dry_run: bool) -> None:
    additions = []
    if "specs" not in existing:
        additions.append("ALTER TABLE products ADD COLUMN specs TEXT")
    if "lead_time_days" not in existing:
        additions.append("ALTER TABLE products ADD COLUMN lead_time_days INTEGER DEFAULT 3")
    for statement in additions:
        if dry_run:
            print(f"[dry-run] {statement}")
        else:
            conn.execute(statement)


# 只有这些品类才有"温标/容量/防水"意义上的规格。描述兜底必须限定在
# 这个范围内，否则"保温杯 500ml"会被读成"背包容量 0.5L"这种错数据，
# 而错数据比没数据更危险——它会让校验器给出理直气壮的错结论。
SPEC_BEARING_KEYWORDS = ("睡袋", "帐篷", "背包", "冲锋衣", "防潮垫", "登山鞋", "跑步鞋")


def _specs_for_row(name: str, description: str, category: str) -> dict:
    """目录优先，描述兜底，都没有就留空（空 = 规格未知，不猜）。"""
    known = specs_for(name)
    if known is not None:
        return dict(known)
    if not any(word in str(name or "") for word in SPEC_BEARING_KEYWORDS):
        return {}
    return parse_specs(description or "", name)


def migrate(db_path: Path, dry_run: bool = False) -> dict:
    if not db_path.exists():
        raise SystemExit(f"数据库不存在: {db_path}")

    if not dry_run:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = Path(f"{db_path}.backup-specs-{stamp}")
        shutil.copy2(db_path, backup)
        print(f"已备份 -> {backup.name}")

    conn = sqlite3.connect(db_path)
    try:
        existing = _columns(conn)
        if not existing:
            raise SystemExit("products 表不存在，先跑 data/init_db.py")

        _add_columns(conn, existing, dry_run)
        # 建列之后重新读一次，后面的回填要按实际存在的列写。
        columns = existing | {"specs", "lead_time_days"}
        has_specs = "specs" in existing and not dry_run
        select = (
            "SELECT name, description, category, specs, lead_time_days FROM products"
            if has_specs
            else "SELECT name, description, category, NULL, NULL FROM products"
        )

        rows = list(conn.execute(select))
        filled = skipped = 0
        for name, description, category, current_specs, current_lead in rows:
            specs = _specs_for_row(name, description, category)
            if not specs:
                skipped += 1
                continue
            payload = json.dumps(specs, ensure_ascii=False, sort_keys=True)
            if current_specs == payload:
                continue
            filled += 1
            if dry_run:
                print(f"[dry-run] {name} -> {payload} (lead={lead_time_for(category)})")
                continue
            conn.execute(
                "UPDATE products SET specs = ?, lead_time_days = ? WHERE name = ?",
                (payload, lead_time_for(category), name),
            )

        if not dry_run:
            conn.commit()
        print(
            f"{'[dry-run] ' if dry_run else ''}"
            f"共 {len(rows)} 行：回填 {filled}，无规格 {skipped}，"
            f"列 = {sorted(columns & {'specs', 'lead_time_days'})}"
        )
        return {"total": len(rows), "filled": filled, "skipped": skipped}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="给 products 表补规格与采购时效")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="数据库路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的改动")
    args = parser.parse_args()
    migrate(Path(args.db), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
