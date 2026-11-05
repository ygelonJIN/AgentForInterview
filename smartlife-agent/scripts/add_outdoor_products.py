#!/usr/bin/env python3
"""补几件户外商品：填掉库里一直缺的品类，并给露营三大件一个高档备选。

为什么要补：

- 规则会要求"防潮垫""头灯"，而商品库里没有，露营场景必然报"未匹配"；
- 每类只有一件商品时，"预算有余额就换更好的款"没有东西可换。

本脚本只做追加，按名称判重，重复执行安全；改库前自动备份。

用法（项目根目录）::

    python smartlife-agent/scripts/add_outdoor_products.py --dry-run
    python smartlife-agent/scripts/add_outdoor_products.py
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.product_specs import lead_time_for, specs_for  # noqa: E402

DB = ROOT / "data" / "products.db"

# (name, category, subcategory, price, waterproof, brand, stock, description, rating)
NEW_PRODUCTS = [
    ("蛋巢防潮垫", "户外", "露营", 89.0, 0, "挪客", 120, "蛋巢结构，R 值 2.2，单人露营够用", 4.4),
    ("充气加厚防潮垫", "户外", "露营", 259.0, 1, "挪客", 60, "充气式 R 值 4.0，冬季露营不返潮", 4.6),
    ("户外头灯", "户外", "露营", 99.0, 1, "火枫", 150, "300 流明，IPX6 防水，营地夜间照明", 4.5),
    ("高亮营地灯", "户外", "露营", 199.0, 1, "火枫", 80, "800 流明，可当充电宝，营地通用照明", 4.6),
    ("三人自动帐篷", "户外", "露营", 1299.0, 1, "牧高笛", 40, "三秒速开，容纳三人，双层防雨", 4.7),
    ("冬季羽绒睡袋", "户外", "露营", 899.0, 1, "黑冰", 50, "鹅绒填充，舒适温度 -15°C，冬季露营", 4.8),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="补充户外商品")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not DB.exists():
        raise SystemExit(f"数据库不存在: {DB}")

    if not args.dry_run:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(DB, Path(f"{DB}.backup-products-{stamp}"))
        print(f"已备份 -> {DB.name}.backup-products-{stamp}")

    conn = sqlite3.connect(DB)
    try:
        existing = {row[0] for row in conn.execute("SELECT name FROM products")}
        added = 0
        for name, category, subcategory, price, waterproof, brand, stock, desc, rating in NEW_PRODUCTS:
            if name in existing:
                continue
            specs = specs_for(name) or {}
            if args.dry_run:
                print(f"[dry-run] + {name} ¥{price}")
                added += 1
                continue
            conn.execute(
                "INSERT INTO products (name, category, subcategory, price, waterproof, brand,"
                " stock, description, rating, specs, lead_time_days)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (name, category, subcategory, price, waterproof, brand, stock, desc, rating,
                 json.dumps(specs, ensure_ascii=False, sort_keys=True), lead_time_for(category)),
            )
            added += 1
        if not args.dry_run:
            conn.commit()
        print(f"{'[dry-run] ' if args.dry_run else ''}新增 {added} 件，库里现有 {len(existing) + added} 件")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
