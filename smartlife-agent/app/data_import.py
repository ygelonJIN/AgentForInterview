"""结构化 CSV 数据导入、数据库预检和事务写入。"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set


class DataImportError(ValueError):
    """导入数据不合法。"""


@dataclass
class ImportResult:
    table: str
    source: str
    imported: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    rolled_back: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "source": self.source,
            "imported": self.imported,
            "skipped": self.skipped,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "rolled_back": self.rolled_back,
        }


CSV_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "products": {
        "required": {"name", "category", "price"},
        "columns": {
            "id", "name", "category", "subcategory", "price", "waterproof",
            "brand", "stock", "description", "rating",
        },
        "integer": {"id", "stock"},
        "real": {"price", "rating"},
        "boolean": {"waterproof"},
    },
    "reviews": {
        "required": {"product_id", "user_id", "content"},
        "columns": {"id", "product_id", "user_id", "content", "rating"},
        "integer": {"id", "product_id", "rating"},
        "real": set(),
        "boolean": set(),
    },
    "orders": {
        "required": {"user_id", "product_id"},
        "columns": {
            "id", "user_id", "product_id", "quantity", "total_price", "status",
        },
        "integer": {"id", "product_id", "quantity"},
        "real": {"total_price"},
        "boolean": set(),
    },
    "users": {
        "required": {"id", "name"},
        "columns": {"id", "name", "age", "preferences", "budget"},
        "integer": {"age"},
        "real": {"budget"},
        "boolean": set(),
    },
}

_ORDER_STATUSES = {"pending", "shipped", "completed", "cancelled"}


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _coerce_value(key: str, value: str, schema: Mapping[str, Any], row_number: int) -> Any:
    raw = (value or "").strip()
    if raw == "":
        return None
    if key in schema["integer"]:
        try:
            return int(raw)
        except ValueError as exc:
            raise DataImportError(f"第 {row_number} 行 {key} 必须是整数: {raw}") from exc
    if key in schema["real"]:
        try:
            return float(raw)
        except ValueError as exc:
            raise DataImportError(f"第 {row_number} 行 {key} 必须是数字: {raw}") from exc
    if key in schema["boolean"]:
        normalized = raw.casefold()
        if normalized in {"1", "true", "yes", "y", "是"}:
            return 1
        if normalized in {"0", "false", "no", "n", "否"}:
            return 0
        raise DataImportError(f"第 {row_number} 行 {key} 必须是布尔值: {raw}")
    return raw


def _validate_table(table: str) -> Mapping[str, Any]:
    if table not in CSV_SCHEMAS:
        raise DataImportError(f"不支持的表: {table}，可选: {', '.join(CSV_SCHEMAS)}")
    return CSV_SCHEMAS[table]


def _validate_row_constraints(table: str, row: Mapping[str, Any], row_number: int) -> None:
    def number(key: str) -> Optional[float]:
        value = row.get(key)
        return None if value is None else float(value)

    if number("price") is not None and number("price") < 0:
        raise DataImportError(f"第 {row_number} 行 price 不能为负数")
    if number("stock") is not None and number("stock") < 0:
        raise DataImportError(f"第 {row_number} 行 stock 不能为负数")
    if number("rating") is not None and not 0 <= number("rating") <= 5:
        raise DataImportError(f"第 {row_number} 行 rating 必须在 0 到 5 之间")
    if number("age") is not None and number("age") < 0:
        raise DataImportError(f"第 {row_number} 行 age 不能为负数")
    if number("budget") is not None and number("budget") < 0:
        raise DataImportError(f"第 {row_number} 行 budget 不能为负数")
    if table == "reviews" and row.get("rating") is not None and not 1 <= int(row["rating"]) <= 5:
        raise DataImportError(f"第 {row_number} 行 rating 必须在 1 到 5 之间")
    if table == "orders":
        if row.get("quantity") is not None and int(row["quantity"]) <= 0:
            raise DataImportError(f"第 {row_number} 行 quantity 必须大于 0")
        if number("total_price") is not None and number("total_price") < 0:
            raise DataImportError(f"第 {row_number} 行 total_price 不能为负数")
        status = row.get("status")
        if status is not None and str(status).strip().casefold() not in _ORDER_STATUSES:
            raise DataImportError(
                f"第 {row_number} 行 status 必须是 {', '.join(sorted(_ORDER_STATUSES))}"
            )


def load_csv_rows(
    csv_path: str | Path,
    table: str,
    *,
    delimiter: str = ",",
) -> List[Dict[str, Any]]:
    """读取并校验 CSV，不写数据库。"""
    schema = _validate_table(table)
    source = Path(csv_path)
    if not source.exists():
        raise DataImportError(f"CSV 文件不存在: {source}")

    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if not reader.fieldnames:
            raise DataImportError("CSV 缺少表头")
        headers = {name.strip() for name in reader.fieldnames if name}
        unknown = headers - schema["columns"]
        if unknown:
            raise DataImportError(f"CSV 包含未知字段: {', '.join(sorted(unknown))}")
        missing = schema["required"] - headers
        if missing:
            raise DataImportError(f"CSV 缺少必填字段: {', '.join(sorted(missing))}")

        rows: List[Dict[str, Any]] = []
        for row_number, raw_row in enumerate(reader, start=2):
            row = {
                key.strip(): _coerce_value(key.strip(), value or "", schema, row_number)
                for key, value in raw_row.items()
                if key and key.strip() in schema["columns"]
            }
            if not any(value is not None for value in row.values()):
                continue
            for key in schema["required"]:
                if row.get(key) is None:
                    raise DataImportError(f"第 {row_number} 行缺少必填字段: {key}")
            _validate_row_constraints(table, row, row_number)
            rows.append(row)
    return rows


def _table_schema(conn: sqlite3.Connection, table: str) -> Dict[str, Any]:
    quoted = _quote_identifier(table)
    columns = {
        row["name"]: dict(row)
        for row in conn.execute(f"PRAGMA table_info({quoted})").fetchall()
    }
    if not columns:
        raise DataImportError(f"数据库不存在目标表: {table}")
    return columns


def preflight_import_rows(
    db_path: str | Path,
    table: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    replace_existing: bool = False,
) -> ImportResult:
    """在不写数据库的情况下检查表结构、主键冲突和外键。"""
    schema = _validate_table(table)
    result = ImportResult(table=table, source=str(db_path))
    materialized = [dict(row) for row in rows]
    if not materialized:
        return result

    database = Path(db_path)
    if not database.exists():
        raise DataImportError(f"数据库文件不存在: {database}")

    uri = f"{database.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise DataImportError(f"数据库打开失败: {exc}") from exc

    with conn:
        conn.row_factory = sqlite3.Row
        columns = _table_schema(conn, table)
        csv_columns = set().union(*(row.keys() for row in materialized))
        missing_db = schema["required"] - set(columns)
        if missing_db:
            result.errors.append(
                f"数据库表缺少字段: {', '.join(sorted(missing_db))}"
            )
        unknown_db = csv_columns - set(columns)
        if unknown_db:
            result.errors.append(
                f"数据库表不支持字段: {', '.join(sorted(unknown_db))}"
            )

        foreign_keys = [dict(row) for row in conn.execute(
            f"PRAGMA foreign_key_list({_quote_identifier(table)})"
        ).fetchall()]
        seen_ids: Set[Any] = set()
        for row_number, row in enumerate(materialized, start=2):
            row_id = row.get("id")
            if row_id is not None:
                if row_id in seen_ids:
                    result.errors.append(f"第 {row_number} 行 ID 在文件中重复: {row_id}")
                seen_ids.add(row_id)
                existing = conn.execute(
                    f"SELECT 1 FROM {_quote_identifier(table)} WHERE id = ? LIMIT 1",
                    (row_id,),
                ).fetchone()
                if existing and not replace_existing:
                    result.errors.append(f"第 {row_number} 行 ID 已存在: {row_id}")
                elif existing:
                    result.warnings.append(f"第 {row_number} 行将覆盖已有 ID: {row_id}")

            for foreign_key in foreign_keys:
                child = foreign_key.get("from")
                parent_table = foreign_key.get("table")
                parent_column = foreign_key.get("to") or "id"
                value = row.get(child)
                if value is None or not parent_table:
                    continue
                exists = conn.execute(
                    f"SELECT 1 FROM {_quote_identifier(parent_table)} "
                    f"WHERE {_quote_identifier(parent_column)} = ? LIMIT 1",
                    (value,),
                ).fetchone()
                if not exists:
                    result.errors.append(
                        f"第 {row_number} 行 {child}={value} 在 {parent_table}.{parent_column} 中不存在"
                    )
    conn.close()
    return result


def preflight_csv_import(
    db_path: str | Path,
    table: str,
    csv_path: str | Path,
    *,
    delimiter: str = ",",
    replace_existing: bool = False,
) -> ImportResult:
    rows = load_csv_rows(csv_path, table, delimiter=delimiter)
    result = preflight_import_rows(
        db_path,
        table,
        rows,
        replace_existing=replace_existing,
    )
    result.source = str(csv_path)
    return result


def _upsert_sql(table: str, fields: Sequence[str], replace_existing: bool) -> str:
    columns = ", ".join(_quote_identifier(field) for field in fields)
    placeholders = ", ".join("?" for _ in fields)
    base = (
        f"INSERT INTO {_quote_identifier(table)} ({columns}) "
        f"VALUES ({placeholders})"
    )
    if not replace_existing or "id" not in fields:
        return base
    updates = [
        f"{_quote_identifier(field)} = excluded.{_quote_identifier(field)}"
        for field in fields
        if field != "id"
    ]
    if not updates:
        return base
    return base + " ON CONFLICT(id) DO UPDATE SET " + ", ".join(updates)


def import_csv_rows(
    db_path: str | Path,
    table: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    source: str = "csv",
    replace_existing: bool = False,
    allow_partial: bool = False,
    preflight: bool = True,
) -> ImportResult:
    """将记录导入 SQLite；默认整个文件事务化，失败时全部回滚。"""
    _validate_table(table)
    result = ImportResult(table=table, source=source)
    materialized = [dict(row) for row in rows]
    if not materialized:
        return result

    if preflight:
        result = preflight_import_rows(
            db_path,
            table,
            materialized,
            replace_existing=replace_existing,
        )
        result.source = source
        if result.errors and not allow_partial:
            result.skipped = len(materialized)
            result.rolled_back = True
            return result

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        row_errors = 0
        for row_number, row in enumerate(materialized, start=2):
            fields = [key for key in row if key in CSV_SCHEMAS[table]["columns"] and row[key] is not None]
            values = [row[key] for key in fields]
            if not fields:
                result.skipped += 1
                continue
            try:
                conn.execute(_upsert_sql(table, fields, replace_existing), values)
                result.imported += 1
            except sqlite3.Error as exc:
                row_errors += 1
                result.errors.append(f"第 {row_number} 行: {exc}")
                result.skipped += 1
        if row_errors and not allow_partial:
            conn.rollback()
            result.imported = 0
            result.skipped = len(materialized)
            result.rolled_back = True
        else:
            conn.commit()
    return result


def import_csv(
    db_path: str | Path,
    table: str,
    csv_path: str | Path,
    *,
    delimiter: str = ",",
    replace_existing: bool = False,
    allow_partial: bool = False,
) -> ImportResult:
    rows = load_csv_rows(csv_path, table, delimiter=delimiter)
    return import_csv_rows(
        db_path,
        table,
        rows,
        source=str(csv_path),
        replace_existing=replace_existing,
        allow_partial=allow_partial,
    )


def write_csv_template(destination: str | Path, table: str) -> Path:
    _validate_table(table)
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    preferred = {
        "products": ["id", "name", "category", "subcategory", "price", "waterproof", "brand", "stock", "description", "rating"],
        "reviews": ["id", "product_id", "user_id", "content", "rating"],
        "orders": ["id", "user_id", "product_id", "quantity", "total_price", "status"],
        "users": ["id", "name", "age", "preferences", "budget"],
    }[table]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=preferred)
        writer.writeheader()
    return path
