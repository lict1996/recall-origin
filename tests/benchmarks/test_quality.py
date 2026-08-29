from __future__ import annotations

from pathlib import Path

from recall_origin.benchmarks.quality import run_quality_suite


def test_required_quality_cases_pass_with_raw_expected_and_actual_results(
    tmp_path: Path,
) -> None:
    result = run_quality_suite(tmp_path)

    assert result["passed"] is True
    assert result["passed_count"] == result["case_count"] == 4
    assert {case["case_id"] for case in result["cases"]} == {
        "exact_and_lexical",
        "bitemporal_valid_and_known_time",
        "partition_isolation",
        "deletion_non_resurrection",
    }
    for case in result["cases"]:
        assert case["passed"] is True
        assert case["expected"]
        assert case["actual"]
