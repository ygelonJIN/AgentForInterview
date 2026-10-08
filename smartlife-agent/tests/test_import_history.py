from app.import_history import ImportHistory


def test_import_history_records_and_lists_jobs(tmp_path):
    history = ImportHistory(tmp_path / "import_history.db")
    first = history.record(
        kind="database",
        target="products",
        source="products.csv",
        status="completed",
        imported=2,
        skipped=0,
        result={"table": "products"},
    )
    history.record(
        kind="rag",
        target="reviews",
        source="reviews.jsonl",
        status="failed",
        skipped=1,
        result={"error": "bad json"},
    )

    rows = history.list_recent()
    assert first["id"] == rows[1]["id"]
    assert rows[0]["kind"] == "rag"
    assert rows[0]["result"]["error"] == "bad json"
    assert rows[1]["imported"] == 2
