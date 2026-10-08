"""在线 A/B 记录与导出测试。"""

import importlib.util
import sqlite3
from pathlib import Path

from app.evaluation.ab_logging import ExperimentLogger


def test_experiment_logger_records_outcomes_and_summary(tmp_path):
    logger = ExperimentLogger(tmp_path / "ab.db", salt="test", experiment="rerank-test")

    variant = logger.assign_variant("user-1", "防水跑步鞋")
    logger.log_impression(
        user_id="user-1",
        session_id="session-1",
        query="防水跑步鞋",
        variant=variant,
        ranked_document_ids=["product-9", "product-20"],
    )
    logger.log_outcome(
        user_id="user-1",
        session_id="session-1",
        query="防水跑步鞋",
        event_type="click",
        rank=0,
        document_id="product-9",
        variant=variant,
    )

    summary = logger.summary()
    values = summary["variants"][variant]

    assert values["impressions"] == 1
    assert values["clicks"] == 1
    assert values["click_through_rate"] == 1.0
    with sqlite3.connect(logger.db_path) as connection:
        row = connection.execute("SELECT subject_hash FROM ranking_events LIMIT 1").fetchone()
        assert row[0] != "user-1"


def test_export_ab_ranking_cases_uses_clicked_documents(tmp_path):
    path = Path(__file__).parents[1] / "scripts" / "export_ab_ranking_cases.py"
    spec = importlib.util.spec_from_file_location("export_ab", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    logger = ExperimentLogger(tmp_path / "ab.db", salt="test", experiment="rerank-test")
    variant = logger.assign_variant("user-1", "防水跑步鞋")
    logger.log_impression(
        user_id="user-1",
        session_id="session-1",
        query="防水跑步鞋",
        variant=variant,
        ranked_document_ids=["product-9", "product-20"],
    )
    logger.log_outcome(
        user_id="user-1",
        session_id="session-1",
        query="防水跑步鞋",
        event_type="conversion",
        rank=0,
        document_id="product-9",
        variant=variant,
    )

    output = tmp_path / "cases.jsonl"
    original_argv = module.sys.argv
    module.sys.argv = [
        "export",
        "--db", str(logger.db_path),
        "--experiment", "rerank-test",
        "--output", str(output),
    ]
    try:
        assert module.main() == 0
    finally:
        module.sys.argv = original_argv

    payload = output.read_text(encoding="utf-8")
    assert "防水跑步鞋" in payload
    assert "product-9" in payload
