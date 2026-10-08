#!/usr/bin/env python3
"""SmartLife Agent 统一数据导入 CLI。"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.data_import import (  # noqa: E402
    CSV_SCHEMAS,
    DataImportError,
    import_csv,
    load_csv_rows,
    preflight_csv_import,
    write_csv_template,
)
from app.document_import import SUPPORTED_DOCUMENT_SUFFIXES, load_document_records  # noqa: E402


def _parse_metadata(items: list[str]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--metadata 格式应为 key=value: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"--metadata 缺少字段名: {item}")
        if value.strip().isdigit():
            metadata[key] = int(value.strip())
        else:
            metadata[key] = value.strip()
    return metadata


def _record_history(kind: str, target: str, source: str, status: str, result: dict) -> None:
    try:
        from app.import_history import ImportHistory

        ImportHistory().record(
            kind=kind,
            target=target,
            source=source,
            status=status,
            result=result,
            imported=int(result.get("imported", result.get("added", {}).get("chunks", 0)) or 0),
            skipped=int(result.get("skipped", len(result.get("added", {}).get("skipped", []))) or 0),
            deleted=int(result.get("deleted", {}).get("deleted", 0) or 0),
            chunks=int(result.get("added", {}).get("chunks", 0) or 0),
        )
    except Exception:
        # 审计记录不能阻断导入本身；UI 仍会显示实时结果。
        pass


def command_db(args: argparse.Namespace) -> int:
    rows = load_csv_rows(args.file, args.table, delimiter=args.delimiter)
    if args.dry_run:
        result = preflight_csv_import(
            args.database,
            args.table,
            args.file,
            delimiter=args.delimiter,
            replace_existing=args.replace_existing,
        ).to_dict()
        result.update({"valid_rows": len(rows), "dry_run": True})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result["errors"] else 2

    result = import_csv(
        args.database,
        args.table,
        args.file,
        delimiter=args.delimiter,
        replace_existing=args.replace_existing,
        allow_partial=args.allow_partial,
    )
    payload = result.to_dict()
    _record_history(
        "database",
        args.table,
        args.file,
        "failed" if result.errors else "completed",
        payload,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not result.errors else 2


def command_rag(args: argparse.Namespace) -> int:
    from app.retrieval.rag import RAGRetriever

    file_paths = [str(Path(path).resolve()) for path in args.files]
    if args.dry_run:
        metadata = _parse_metadata(args.metadata)
        checks = []
        for path in file_paths:
            item = {"file": path}
            try:
                records = load_document_records(path, args.source_type, metadata)
                item.update({
                    "supported": True,
                    "records": len(records),
                    "characters": sum(len(record.page_content) for record in records),
                })
            except Exception as exc:
                item.update({
                    "supported": False,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            checks.append(item)
        result = {
            "mode": args.mode,
            "source_type": args.source_type,
            "checks": checks,
            "metadata": metadata,
            "dry_run": True,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if all(item["supported"] for item in checks) else 2

    rag = RAGRetriever()
    metadata = _parse_metadata(args.metadata)
    output = []
    errors = 0
    for file_path in file_paths:
        try:
            if args.mode == "sync":
                output.append(rag.sync_file(file_path, args.source_type, metadata=metadata))
            else:
                output.append({
                    "source": file_path,
                    "added": rag.add_documents(
                        [file_path], source_type=args.source_type, metadata=metadata
                    ),
                })
        except Exception as exc:
            errors += 1
            output.append({
                "source": file_path,
                "error": f"{type(exc).__name__}: {exc}",
            })
    output.append({"collection": rag.get_collection_stats()})
    for item in output[:-1]:
        added = item.get("added") or {}
        deleted = item.get("deleted") or {}
        _record_history(
            "rag",
            args.source_type,
            item.get("source", item.get("original_name", "unknown")),
            "failed" if added.get("skipped") else "completed",
            item,
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 2 if errors else 0


def command_vectors(args: argparse.Namespace) -> int:
    from app.retrieval.rag import rebuild_vector_store

    if not args.rebuild:
        print(json.dumps({"error": "必须显式指定 --rebuild"}, ensure_ascii=False))
        return 2
    result = rebuild_vector_store(
        data_dir=args.data_dir,
        persist_dir=args.persist_dir,
        collection_name=args.collection,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _infer_source_type(path: Path, data_dir: Path) -> Optional[str]:
    try:
        relative = path.resolve().relative_to(data_dir.resolve())
    except ValueError:
        return None
    parts = relative.parts
    for candidate in ("reviews", "guides", "activities"):
        if candidate in parts:
            return candidate
    return None


def command_watch(args: argparse.Namespace) -> int:
    try:
        from watchfiles import watch
    except ImportError as exc:
        raise RuntimeError("实时监听需要 watchfiles，请安装 requirements.txt") from exc

    from app.retrieval.rag import RAGRetriever

    data_dir = Path(args.data_dir).resolve()
    raw_paths = list(args.paths or []) + list(getattr(args, "path_options", []) or [])
    if not raw_paths:
        raw_paths = [
            str(data_dir / "reviews"),
            str(data_dir / "guides"),
            str(data_dir / "activities"),
            str(data_dir / "imports"),
        ]
    paths = [Path(path).resolve() for path in raw_paths]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)

    rag = RAGRetriever(
        data_dir=str(data_dir),
        persist_dir=args.persist_dir,
        collection_name=args.collection,
    )
    print(json.dumps({
        "watching": [str(path) for path in paths],
        "mode": args.mode,
        "debounce_ms": args.debounce_ms,
    }, ensure_ascii=False), flush=True)
    metadata = _parse_metadata(args.metadata)
    for changes in watch(*paths, debounce=args.debounce_ms):
        for _change_type, raw_path in changes:
            path = Path(raw_path)
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_DOCUMENT_SUFFIXES:
                continue
            source_type = _infer_source_type(path, data_dir)
            if not source_type:
                print(json.dumps({
                    "skipped": str(path),
                    "reason": "无法从路径推断 reviews/guides/activities",
                }, ensure_ascii=False), flush=True)
                continue
            try:
                if args.mode == "sync":
                    result = rag.sync_file(str(path), source_type, metadata=metadata)
                else:
                    result = {
                        "source": str(path),
                        "added": rag.add_documents(
                            [str(path)], source_type=source_type, metadata=metadata
                        ),
                    }
                print(json.dumps(result, ensure_ascii=False), flush=True)
            except Exception as exc:
                print(json.dumps({
                    "source": str(path),
                    "error": f"{type(exc).__name__}: {exc}",
                }, ensure_ascii=False), flush=True)
    return 0


def command_template(args: argparse.Namespace) -> int:
    destination = Path(args.destination or f"data/import_templates/{args.table}.csv")
    if not destination.is_absolute():
        destination = PROJECT_ROOT / destination
    path = write_csv_template(destination, args.table)
    print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="导入结构化 CSV 或 RAG CSV/TXT/Markdown/JSONL/PDF/DOCX/Excel 数据",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    db = subparsers.add_parser("db", help="导入商品、评价、订单或用户 CSV")
    db.add_argument("--table", required=True, choices=sorted(CSV_SCHEMAS))
    db.add_argument("--file", required=True)
    db.add_argument("--database", default=str(PROJECT_ROOT / "data" / "products.db"))
    db.add_argument("--delimiter", default=",")
    db.add_argument("--replace-existing", action="store_true")
    db.add_argument("--allow-partial", action="store_true")
    db.add_argument("--dry-run", action="store_true")
    db.set_defaults(func=command_db)

    rag = subparsers.add_parser("rag", help="增量导入或替换 RAG 文档（含 PDF/DOCX/Excel）")
    rag.add_argument("files", nargs="+")
    rag.add_argument(
        "--source-type",
        required=True,
        choices=["reviews", "guides", "activities"],
    )
    rag.add_argument(
        "--mode",
        choices=["sync", "add"],
        default="sync",
        help="sync 会先删除同来源旧向量，add 只追加",
    )
    rag.add_argument(
        "--metadata",
        action="append",
        default=[],
        help="附加元数据，例如 --metadata product_id=31",
    )
    rag.add_argument("--dry-run", action="store_true")
    rag.set_defaults(func=command_rag)

    vectors = subparsers.add_parser(
        "vectors",
        help="Embedding 更换后全量重建向量库",
    )
    vectors.add_argument("--rebuild", action="store_true", help="确认删除旧向量并重建")
    vectors.add_argument("--data-dir", default=str(PROJECT_ROOT / "data"))
    vectors.add_argument("--persist-dir", default=str(PROJECT_ROOT / "chroma_db"))
    vectors.add_argument("--collection", default="smartlife")
    vectors.set_defaults(func=command_vectors)

    watcher = subparsers.add_parser("watch", help="实时监听知识目录并同步向量")
    watcher.add_argument(
        "paths",
        nargs="*",
    )
    watcher.add_argument(
        "--paths",
        dest="path_options",
        action="append",
        default=[],
        help="可重复指定监听目录，也支持位置参数",
    )
    watcher.add_argument("--data-dir", default=str(PROJECT_ROOT / "data"))
    watcher.add_argument("--persist-dir", default=str(PROJECT_ROOT / "chroma_db"))
    watcher.add_argument("--collection", default="smartlife")
    watcher.add_argument("--mode", choices=["sync", "add"], default="sync")
    watcher.add_argument("--metadata", action="append", default=[])
    watcher.add_argument("--debounce-ms", type=int, default=1000)
    watcher.set_defaults(func=command_watch)

    template = subparsers.add_parser("template", help="生成 CSV 导入模板")
    template.add_argument("--table", required=True, choices=sorted(CSV_SCHEMAS))
    template.add_argument("--destination")
    template.set_defaults(func=command_template)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (DataImportError, ValueError, FileNotFoundError, RuntimeError) as exc:
        parser.exit(2, f"导入失败: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
