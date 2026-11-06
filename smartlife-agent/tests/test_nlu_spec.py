"""NLU 场景集：把"这句话应该判成什么"固化成可回归的数据。"""

from pathlib import Path

from app.evaluation.nlu_spec import DEFAULT_SCENARIOS, evaluate, format_report, load_cases


def test_scenario_file_exists_and_is_diverse():
    cases = load_cases(Path(DEFAULT_SCENARIOS))

    assert len(cases) >= 40
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids)), "场景 id 不能重复"
    kinds = {case["id"].split("-")[0] for case in cases}
    assert {"shop", "travel", "composite", "service", "gear", "slot", "misc"} <= kinds


def test_all_scenarios_match_current_rules():
    report = evaluate(load_cases(Path(DEFAULT_SCENARIOS)))

    assert report["summary"]["mismatched"] == 0, "\n" + format_report(report)
