from pathlib import Path

import sqlite3

import pytest

from app.data_import import DataImportError, import_csv, load_csv_rows


def _database(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            subcategory TEXT,
            price REAL NOT NULL,
            waterproof BOOLEAN,
            brand TEXT,
            stock INTEGER,
            description TEXT,
            rating REAL
        );
        CREATE TABLE reviews (
            id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            rating INTEGER,
            FOREIGN KEY (product_id) REFERENCES products(id)
        );
        """
    )
    conn.commit()
    conn.close()


def test_csv_import_validates_and_inserts_rows(tmp_path):
    db_path = tmp_path / "products.db"
    _database(db_path)
    csv_path = tmp_path / "products.csv"
    csv_path.write_text(
        "name,category,subcategory,price,waterproof,brand,stock,description,rating\n"
        "登山背包,户外,登山,399,是,Example,80,轻量登山背包,4.6\n",
        encoding="utf-8",
    )

    rows = load_csv_rows(csv_path, "products")
    assert rows[0]["waterproof"] == 1
    result = import_csv(db_path, "products", csv_path)

    assert result.imported == 1
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT name, price, waterproof FROM products WHERE name = ?",
            ("登山背包",),
        ).fetchone()
    assert row == ("登山背包", 399.0, 1)


def test_csv_import_rejects_unknown_and_missing_columns(tmp_path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("name,category\n商品,户外\n", encoding="utf-8")
    with pytest.raises(DataImportError, match="price"):
        load_csv_rows(csv_path, "products")

    csv_path.write_text("name,category,price,unknown\n商品,户外,10,x\n", encoding="utf-8")
    with pytest.raises(DataImportError, match="unknown"):
        load_csv_rows(csv_path, "products")


def test_preflight_checks_foreign_keys_and_existing_ids(tmp_path):
    from app.data_import import preflight_import_rows

    db_path = tmp_path / "products.db"
    _database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO products (id, name, category, price) VALUES (1, '跑鞋', '服装', 499)"
        )
        conn.execute(
            "INSERT INTO reviews (id, product_id, user_id, content, rating) "
            "VALUES (1, 1, 'old-user', '已有评价', 5)"
        )
        conn.commit()

    result = preflight_import_rows(
        db_path,
        "reviews",
        [
            {"product_id": 999, "user_id": "u1", "content": "不存在的商品", "rating": 5},
            {"id": 1, "product_id": 1, "user_id": "u1", "content": "重复 ID", "rating": 5},
            {"id": 1, "product_id": 1, "user_id": "u1", "content": "再次重复", "rating": 5},
        ],
    )

    assert any("product_id=999" in error for error in result.errors)
    assert any("ID 已存在" in error for error in result.errors)
    assert any("ID 在文件中重复" in error for error in result.errors)


def test_default_import_is_atomic_and_upsert_updates_existing_row(tmp_path):
    from app.data_import import import_csv_rows

    db_path = tmp_path / "products.db"
    _database(db_path)

    failed = import_csv_rows(
        db_path,
        "products",
        [
            {"name": "有效商品", "category": "户外", "price": 10.0},
            {"name": "缺少价格", "category": "户外"},
        ],
        preflight=False,
    )
    assert failed.rolled_back is True
    assert failed.imported == 0
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0

    import_csv_rows(
        db_path,
        "products",
        [{"id": 7, "name": "旧名称", "category": "户外", "price": 10.0}],
        preflight=False,
    )
    replaced = import_csv_rows(
        db_path,
        "products",
        [{"id": 7, "name": "新名称", "category": "户外", "price": 20.0}],
        replace_existing=True,
        preflight=False,
    )
    assert replaced.imported == 1
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT name, price FROM products WHERE id = 7").fetchone()
    assert row == ("新名称", 20.0)


def test_init_db_refuses_to_destroy_existing_database_without_force(tmp_path, monkeypatch):
    import importlib.util

    module_path = Path(__file__).parents[1] / "data" / "init_db.py"
    spec = importlib.util.spec_from_file_location("init_db_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir()
    db_path = fake_data_dir / "products.db"
    db_path.write_bytes(b"existing")
    monkeypatch.setattr(module, "__file__", str(fake_data_dir / "init_db.py"))

    try:
        module.init_database(force=False)
    except RuntimeError as exc:
        assert "--force" in str(exc)
    else:
        raise AssertionError("init_database should refuse to overwrite an existing database")
    assert db_path.read_bytes() == b"existing"


def test_watch_source_type_includes_nested_upload_namespaces(tmp_path):
    import importlib.util

    module_path = Path(__file__).parents[1] / "scripts" / "import_data.py"
    spec = importlib.util.spec_from_file_location("import_cli_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    path = tmp_path / "imports" / "reviews" / "user_001" / "review.txt"
    path.parent.mkdir(parents=True)
    path.write_text("评价", encoding="utf-8")

    assert module._infer_source_type(path, tmp_path) == "reviews"
    assert module._infer_source_type(tmp_path / "unknown" / "a.txt", tmp_path) is None


def test_init_db_force_backs_up_existing_database(tmp_path, monkeypatch):
    import importlib.util
    import sqlite3

    module_path = Path(__file__).parents[1] / "data" / "init_db.py"
    spec = importlib.util.spec_from_file_location("init_db_force_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir()
    db_path = fake_data_dir / "products.db"
    db_path.write_bytes(b"old-db")
    monkeypatch.setattr(module, "__file__", str(fake_data_dir / "init_db.py"))

    module.init_database(force=True)

    backups = list(fake_data_dir.glob("products.db.backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"old-db"
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    assert count > 0
