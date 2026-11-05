"""规格迁移脚本的测试：能加列、能回填、重复跑不重复改。

手工迁库最容易出的两个问题——跑第二遍把已有值覆盖成空、以及把
"保温杯 500ml"读成"背包容量"——这里各钉一条。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_product_specs.py"

OLD_SCHEMA = """
CREATE TABLE products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    subcategory TEXT,
    price REAL NOT NULL,
    waterproof BOOLEAN DEFAULT 0,
    brand TEXT,
    stock INTEGER DEFAULT 100,
    description TEXT,
    rating REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def _load_module():
    spec = importlib.util.spec_from_file_location("migrate_product_specs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "products.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.executemany(
        "INSERT INTO products (name, category, subcategory, price, description) VALUES (?,?,?,?,?)",
        [
            ("四季保暖睡袋", "户外", "露营", 299.0, "鸭绒填充，舒适温度-5°C"),
            ("户外保温杯", "户外", "露营", 139.0, "316不锈钢内胆，500ml大容量"),
            ("纯棉圆领T恤", "服装", "男装", 89.0, "100%纯棉面料"),
        ],
    )
    conn.commit()
    conn.close()
    return path


def test_加列并回填目录规格(db_path):
    module = _load_module()
    module.migrate(db_path)

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    assert {"specs", "lead_time_days"} <= columns

    specs = json.loads(
        conn.execute("SELECT specs FROM products WHERE name = '四季保暖睡袋'").fetchone()[0]
    )
    assert specs == {"comfort_temp_c": -5}

    lead = conn.execute(
        "SELECT lead_time_days FROM products WHERE name = '四季保暖睡袋'"
    ).fetchone()[0]
    assert lead == 5
    conn.close()


def test_保温杯的毫升不会被当成背包容量(db_path):
    # 描述兜底只在有规格语义的品类上生效，否则会生成"容量 0.5L"这种错数据。
    module = _load_module()
    module.migrate(db_path)
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT specs FROM products WHERE name = '户外保温杯'"
    ).fetchone()[0] in (None, "", "{}")
    conn.close()


def test_重复执行是幂等的(db_path):
    module = _load_module()
    first = module.migrate(db_path)
    second = module.migrate(db_path)
    assert first["filled"] >= 1
    assert second["filled"] == 0
    # 两遍都留备份，但第二遍不改数据。
    assert len(list(db_path.parent.glob("products.db.backup-specs-*"))) >= 1


def test_库不存在时明确报错(tmp_path):
    module = _load_module()
    with pytest.raises(SystemExit):
        module.migrate(tmp_path / "missing.db")


def test_干跑不落盘(db_path):
    module = _load_module()
    before = db_path.read_bytes()
    module.migrate(db_path, dry_run=True)
    assert db_path.read_bytes() == before
