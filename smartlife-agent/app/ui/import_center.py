"""数据导入中心：数据库 CSV、RAG 文档和模板自助导入。"""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Dict

import streamlit as st

from app.data_import import (
    CSV_SCHEMAS,
    DataImportError,
    import_csv,
    preflight_csv_import,
)
from app.document_import import SUPPORTED_DOCUMENT_SUFFIXES, load_document_records
from app.import_history import ImportHistory
from app.ui.theme import render_empty, render_page_header, render_section_header

PROJECT_ROOT = Path(__file__).resolve().parents[2]
IMPORT_DIR = PROJECT_ROOT / "data" / "imports"
TEMPLATE_DIR = PROJECT_ROOT / "data" / "import_templates"
DB_PATH = PROJECT_ROOT / "data" / "products.db"


def _safe_filename(name: str) -> str:
    name = Path(name or "upload").name
    return re.sub(r"[^0-9A-Za-z._-]", "_", name)[-160:] or "upload"


def _save_upload(
    uploaded,
    *,
    stable: bool = False,
    source_type: str = "database",
    namespace: str = "local",
) -> Path:
    source_type = re.sub(r"[^0-9A-Za-z_-]", "_", source_type or "database") or "database"
    namespace = re.sub(r"[^0-9A-Za-z_.-]", "_", namespace or "local") or "local"
    directory = IMPORT_DIR / source_type / namespace
    directory.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename(uploaded.name)
    suffix = Path(filename).suffix.lower()
    if stable:
        # 同名文件保持同一 source，sync 才能准确替换旧向量；
        # 目录按 source_type 隔离，确保全量重建可以恢复网页上传内容。
        path = directory / filename
    else:
        path = directory / f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:10]}{suffix}"
    if stable and path.exists():
        backup = path.with_name(
            f"{path.name}.backup-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        )
        shutil.copy2(path, backup)
    path.write_bytes(uploaded.getvalue())
    return path


def _parse_metadata(text: str) -> Dict[str, object]:
    metadata: Dict[str, object] = {}
    for item in re.split(r"[\n,]+", text or ""):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"元数据格式应为 key=value：{item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"元数据缺少字段名：{item}")
        value = value.strip()
        lowered = value.casefold()
        if lowered in {"true", "false"}:
            metadata[key] = lowered == "true"
        else:
            try:
                metadata[key] = int(value)
            except ValueError:
                try:
                    metadata[key] = float(value)
                except ValueError:
                    metadata[key] = value
    return metadata


def _render_template_downloads() -> None:
    st.caption("按模板填写后上传；数据库模板是 CSV，RAG 模板支持 CSV、TXT、Markdown、JSONL、PDF、DOCX、Excel。")
    template_files = [
        ("数据库-products.csv", TEMPLATE_DIR / "products.csv"),
        ("数据库-reviews.csv", TEMPLATE_DIR / "reviews.csv"),
        ("数据库-orders.csv", TEMPLATE_DIR / "orders.csv"),
        ("数据库-users.csv", TEMPLATE_DIR / "users.csv"),
        ("RAG-reviews.jsonl", TEMPLATE_DIR / "reviews.jsonl"),
        ("RAG-guides.md", TEMPLATE_DIR / "guides.md"),
        ("RAG-activities.txt", TEMPLATE_DIR / "activities.txt"),
        ("导入说明.md", TEMPLATE_DIR / "README.md"),
    ]
    cols = st.columns(2)
    for index, (label, path) in enumerate(template_files):
        with cols[index % 2]:
            if path.exists():
                st.download_button(
                    label,
                    data=path.read_bytes(),
                    file_name=path.name,
                    mime="text/plain",
                    key=f"download_template_{path.name}",
                    use_container_width=True,
                )
            else:
                st.caption(f"缺少模板：{path.name}")
    st.download_button(
        "数据库建表 SQL（仅用于空库）",
        data=(TEMPLATE_DIR / "schema.sql").read_bytes() if (TEMPLATE_DIR / "schema.sql").exists() else b"",
        file_name="schema.sql",
        mime="text/sql",
        key="download_schema_sql",
        use_container_width=True,
    )


def render_database_import() -> None:
    render_section_header("数据库 CSV 导入")
    st.write("把 CSV 按模板导入现有 SQLite；默认增量 INSERT，不会清空数据库。只有确认覆盖同 ID 时才勾选替换。")
    table = st.selectbox("目标数据表", sorted(CSV_SCHEMAS), key="import_db_table")
    uploaded = st.file_uploader(
        "选择 CSV 文件",
        type=["csv"],
        accept_multiple_files=True,
        key="import_db_files",
    )
    replace_existing = st.checkbox(
        "允许覆盖已有 ID（危险：仅用于明确的更新）",
        key="import_db_replace",
    )
    allow_partial = st.checkbox(
        "允许部分成功（默认关闭：任一行失败会回滚整个文件）",
        key="import_db_partial",
    )
    dry_run = st.checkbox("执行数据库预检，不写数据库", value=True, key="import_db_dry_run")
    if st.button("开始数据库导入", key="import_db_submit", type="primary", use_container_width=True):
        if not uploaded:
            st.warning("请先选择至少一个 CSV 文件。")
            return
        results = []
        progress = st.progress(0, text="正在处理数据库文件…")
        for index, item in enumerate(uploaded, start=1):
            progress.progress(
                (index - 1) / max(len(uploaded), 1),
                text=f"正在处理数据库文件 {index}/{len(uploaded)}：{item.name}",
            )
            path = _save_upload(
                item,
                namespace=st.session_state.get("user_id", "local"),
            )
            try:
                if dry_run:
                    result = preflight_csv_import(
                        DB_PATH,
                        table,
                        path,
                        replace_existing=replace_existing,
                    )
                    payload = result.to_dict()
                    payload["dry_run"] = True
                    results.append(payload)
                else:
                    result = import_csv(
                        DB_PATH,
                        table,
                        path,
                        replace_existing=replace_existing,
                        allow_partial=allow_partial,
                    )
                    results.append(result.to_dict())
            except (DataImportError, OSError, ValueError) as exc:
                results.append({"file": item.name, "error": str(exc)})
        progress.progress(1.0, text="数据库文件处理完成")
        history = ImportHistory()
        for item in results:
            history.record(
                kind="database",
                target=table,
                source=item.get("file", "unknown"),
                status="validated" if dry_run else (
                    "failed" if item.get("errors") or "error" in item else "completed"
                ),
                result=item,
                imported=int(item.get("imported", 0)),
                skipped=int(item.get("skipped", 0)),
            )
        st.json(results)
        st.download_button(
            "下载导入结果 JSON",
            data=json.dumps(results, ensure_ascii=False, indent=2).encode("utf-8"),
            file_name="database-import-result.json",
            mime="application/json",
            key="download_db_import_result",
        )
        if not dry_run and all(item.get("errors") == [] and "error" not in item for item in results):
            st.success("导入完成；数据库内容已更新。")
        elif not dry_run and any(item.get("rolled_back") for item in results):
            st.error("导入失败，文件已整体回滚。")


def render_rag_import() -> None:
    render_section_header("RAG 文档导入")
    st.write(
        "上传文档后会立即执行一次显式同步。系统没有后台实时监听；"
        "也可以继续使用 `scripts/import_data.py rag --mode sync`。"
    )
    st.caption("支持格式：" + "、".join(sorted(SUPPORTED_DOCUMENT_SUFFIXES)))
    st.caption("同名文件覆盖前会保留 `.backup-*` 原文件，便于人工恢复。")
    uploaded = st.file_uploader(
        "选择 RAG 文档",
        type=[suffix.lstrip(".") for suffix in sorted(SUPPORTED_DOCUMENT_SUFFIXES)],
        accept_multiple_files=True,
        key="import_rag_files",
    )
    source_type = st.selectbox("来源分类", ["reviews", "guides", "activities"], key="import_rag_source_type")
    mode = st.radio("导入模式", ["sync", "add"], horizontal=True, key="import_rag_mode")
    metadata_text = st.text_area(
        "附加元数据（每行或逗号分隔 key=value）",
        placeholder="product_id=31\ntenant=demo",
        key="import_rag_metadata",
    )
    dry_run = st.checkbox("只校验文件和格式", value=True, key="import_rag_dry_run")
    if st.button("开始 RAG 导入", key="import_rag_submit", type="primary", use_container_width=True):
        if not uploaded:
            st.warning("请先选择至少一个 RAG 文档。")
            return
        try:
            metadata = _parse_metadata(metadata_text)
        except ValueError as exc:
            st.error(str(exc))
            return
        paths = [
            _save_upload(
                item,
                stable=True,
                source_type=source_type,
                namespace=st.session_state.get("user_id", "local"),
            )
            for item in uploaded
        ]
        if dry_run:
            result = []
            for item, path in zip(uploaded, paths):
                payload = {
                    "file": item.name,
                    "mode": mode,
                    "source_type": source_type,
                    "metadata": metadata,
                    "dry_run": True,
                }
                try:
                    records = load_document_records(path, source_type, metadata)
                    payload.update({
                        "supported": True,
                        "records": len(records),
                        "characters": sum(len(record.page_content) for record in records),
                    })
                except Exception as exc:
                    payload.update({
                        "supported": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                result.append(payload)
            st.json(result)
            st.download_button(
                "下载校验结果 JSON",
                data=json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"),
                file_name="rag-import-validation.json",
                mime="application/json",
                key="download_rag_validation",
            )
            return
        try:
            from app.retrieval.rag import RAGRetriever

            rag = RAGRetriever()
            results = []
            progress = st.progress(0, text="正在准备 RAG 导入…")
            for index, (item, path) in enumerate(zip(uploaded, paths), start=1):
                progress.progress(
                    (index - 1) / max(len(uploaded), 1),
                    text=f"正在导入 RAG 文件 {index}/{len(uploaded)}：{item.name}",
                )
                payload = {
                    "original_name": item.name,
                    "owner": st.session_state.get("user_id", "local"),
                    **metadata,
                }
                try:
                    if mode == "sync":
                        results.append(rag.sync_file(str(path), source_type, metadata=payload))
                    else:
                        results.append({
                            "source": str(path),
                            "original_name": item.name,
                            "added": rag.add_documents([str(path)], source_type=source_type, metadata=payload),
                        })
                except Exception as exc:
                    results.append({
                        "source": str(path),
                        "original_name": item.name,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
            progress.progress(1.0, text="RAG 文件处理完成")
            results.append({"collection": rag.get_collection_stats()})
            history = ImportHistory()
            for item, uploaded_item in zip(results[:-1], uploaded):
                added = item.get("added") or {}
                deleted = item.get("deleted") or {}
                history.record(
                    kind="rag",
                    target=source_type,
                    source=uploaded_item.name,
                    status="failed" if item.get("error") or added.get("skipped") else "completed",
                    result=item,
                    imported=int(added.get("chunks", 0)),
                    skipped=len(added.get("skipped") or []),
                    deleted=int(deleted.get("deleted", 0)),
                    chunks=int(added.get("chunks", 0)),
                )
            st.json(results)
            st.download_button(
                "下载导入结果 JSON",
                data=json.dumps(results, ensure_ascii=False, indent=2).encode("utf-8"),
                file_name="rag-import-result.json",
                mime="application/json",
                key="download_rag_import_result",
            )
            if any("error" in item for item in results[:-1]):
                st.error("部分 RAG 文件导入失败；成功的文件已保留，失败文件请查看结果 JSON。")
            else:
                st.success("RAG 导入完成；向量已持久化。")
        except Exception as exc:
            st.error(f"RAG 导入失败：{type(exc).__name__}: {exc}")


def render_import_history() -> None:
    render_section_header("导入记录", "查看最近的数据库与 RAG 导入任务，便于审计和追踪")
    try:
        jobs = ImportHistory().list_recent(50)
    except Exception as exc:
        st.warning(f"导入记录加载失败：{exc}")
        return
    if not jobs:
        render_empty("还没有导入记录。")
        return
    rows = [
        {
            "时间": item["created_at"],
            "类型": item["kind"],
            "目标": item["target"],
            "来源": item["source"],
            "状态": item["status"],
            "导入": item["imported"],
            "跳过": item["skipped"],
            "删除": item["deleted"],
            "切片": item["chunks"],
        }
        for item in jobs
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.download_button(
        "下载完整导入记录 JSON",
        data=json.dumps(jobs, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name="import-history.json",
        mime="application/json",
        key="download_import_history",
    )


def render_import_page() -> None:
    render_page_header(
        "数据导入",
        "自助导入数据库记录、RAG 文档，并下载标准模板。",
        "Import Center",
    )
    db_tab, rag_tab, template_tab, history_tab = st.tabs(
        ["数据库", "RAG 文档", "模板与说明", "导入记录"]
    )
    with db_tab:
        render_database_import()
    with rag_tab:
        render_rag_import()
    with history_tab:
        render_import_history()
    with template_tab:
        _render_template_downloads()
        st.divider()
        st.markdown(
            """
            #### 使用规则
            - `data/init_db.py` 会删除并重建 `products.db`，只适合空库首次初始化，**不要用于增量导入**。
            - CSV 默认增量写入；同 ID 更新请明确勾选覆盖。
            - RAG `sync` 会替换同一来源文件的旧向量，`add` 只追加。
            - PDF、DOCX、Excel 会在导入时提取文本；扫描版 PDF 需要先做 OCR。
            - 更换 Embedding 模型后不要混用旧向量，应全量重建向量库。
            - 旧的内存对话不会自动迁移；请在个人中心执行“迁移当前会话”。
            """
        )
