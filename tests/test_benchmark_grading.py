"""Tests for resumable human grading of benchmark results."""

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))

from webui import benchmark, benchmark_results, grade  # noqa: E402


def _result_row(**overrides: str) -> dict[str, str]:
    row = {column: "" for column in benchmark_results.BENCHMARK_RESULT_COLUMNS}
    row.update({
        "run_id": "run-1",
        "query_file": "questions.csv",
        "query_file_sha256": "hash-1",
        "query_id": "q1",
        "citygml_dataset": "demo",
        "question": "Which building is tallest?",
        "ground_truth": "Building A\nwith a height of 20 m.",
        "conversation_id": "conversation-1",
        "model_row": "2",
        "provider": "openai",
        "model": "model-a",
        "repetition": "1",
        "generated_answer": r"Building A is tallest.\nIts height is 20 m.",
        "status": "completed",
    })
    row.update(overrides)
    return row


def _write_results(
    path: Path,
    header: tuple[str, ...],
    rows: list[dict[str, str]],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _grade_row(**overrides: str) -> dict[str, str]:
    grade_values = {column: "" for column in benchmark_results.HUMAN_GRADING_COLUMNS}
    grade_values.update({
        "human_correctness": "3",
        "human_completeness": "2",
        "human_relevance": "3",
        "human_clarity": "2",
        "human_comments": "Good answer, but a minor detail is missing.",
    })
    grade_values.update(overrides)
    return grade_values


def test_saving_grade_migrates_legacy_csv_and_round_trips(tmp_path):
    path = tmp_path / "benchmark-results.csv"
    row = _result_row()
    _write_results(path, benchmark_results.BENCHMARK_RESULT_COLUMNS, [row])

    loaded = benchmark_results.load_result_rows(path)
    identity = benchmark_results.result_identity(loaded[0])
    benchmark_results.save_human_grade(path, identity, _grade_row())

    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        saved = next(reader)
        assert tuple(reader.fieldnames or ()) == benchmark_results.RESULT_COLUMNS
    assert all(saved[column] == row[column] for column in row)
    assert all(
        saved[column] == _grade_row()[column]
        for column in benchmark_results.HUMAN_GRADING_COLUMNS
    )
    assert benchmark_results.is_human_graded(saved)
    assert benchmark_results.load_result_rows(path)[0]["generated_answer"] == row["generated_answer"]


def test_benchmark_append_preserves_grades_and_blanks_new_rows(tmp_path):
    path = tmp_path / "benchmark-results.csv"
    original = _result_row()
    _write_results(path, benchmark_results.BENCHMARK_RESULT_COLUMNS, [original])
    loaded = benchmark_results.load_result_rows(path)
    benchmark_results.save_human_grade(
        path,
        benchmark_results.result_identity(loaded[0]),
        _grade_row(),
    )

    stream, writer = benchmark._append_results(path)
    try:
        writer.writerow(_result_row(
            run_id="run-2",
            query_file_sha256="hash-2",
            query_id="q2",
            model_row="3",
            repetition="2",
        ))
        stream.flush()
    finally:
        stream.close()

    rows = benchmark_results.load_result_rows(path)
    assert rows[0]["human_correctness"] == "3"
    assert rows[0]["human_comments"] == "Good answer, but a minor detail is missing."
    assert rows[1]["run_id"] == "run-2"
    assert all(not rows[1][column] for column in benchmark_results.HUMAN_GRADING_COLUMNS)


def test_benchmark_append_migrates_legacy_header(tmp_path):
    path = tmp_path / "legacy-results.csv"
    original = _result_row()
    _write_results(path, benchmark_results.BENCHMARK_RESULT_COLUMNS, [original])

    stream, _ = benchmark._append_results(path)
    stream.close()

    rows = benchmark_results.load_result_rows(path)
    assert rows[0]["query_id"] == original["query_id"]
    assert all(not rows[0][column] for column in benchmark_results.HUMAN_GRADING_COLUMNS)
    with path.open("r", encoding="utf-8", newline="") as stream:
        assert tuple(next(csv.reader(stream))) == benchmark_results.RESULT_COLUMNS


def test_grading_rejects_invalid_scores_and_duplicate_result_identity(tmp_path):
    path = tmp_path / "benchmark-results.csv"
    row = _result_row()
    duplicate = _result_row(generated_answer="A duplicate result.")
    _write_results(
        path,
        benchmark_results.RESULT_COLUMNS,
        [
            {**row, **{column: "" for column in benchmark_results.HUMAN_GRADING_COLUMNS}},
            {**duplicate, **{column: "" for column in benchmark_results.HUMAN_GRADING_COLUMNS}},
        ],
    )

    with pytest.raises(benchmark_results.ResultsCsvError, match="duplicate result identities"):
        benchmark_results.load_result_rows(path)

    single_path = tmp_path / "single.csv"
    _write_results(single_path, benchmark_results.RESULT_COLUMNS, [
        {**row, **{column: "" for column in benchmark_results.HUMAN_GRADING_COLUMNS}},
    ])
    identity = benchmark_results.result_identity(benchmark_results.load_result_rows(single_path)[0])
    with pytest.raises(benchmark_results.ResultsCsvError, match="must be blank"):
        benchmark_results.save_human_grade(single_path, identity, _grade_row(human_clarity="4"))


def test_result_file_reports_unsupported_headers_and_malformed_rows(tmp_path):
    path = tmp_path / "invalid.csv"
    path.write_text("question,generated_answer\nq1,answer\n", encoding="utf-8")
    with pytest.raises(benchmark_results.ResultsCsvError, match="unsupported header"):
        benchmark_results.load_result_rows(path)

    path = tmp_path / "malformed.csv"
    _write_results(path, benchmark_results.RESULT_COLUMNS, [
        {**_result_row(), **{column: "" for column in benchmark_results.HUMAN_GRADING_COLUMNS}},
    ])
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write("short,row\n")
    with pytest.raises(benchmark_results.ResultsCsvError, match="malformed row"):
        benchmark_results.load_result_rows(path)


def test_next_ungraded_navigation_wraps_and_stops_when_all_scored():
    rows = [
        _result_row(human_correctness="3", human_completeness="3", human_relevance="3", human_clarity="3"),
        _result_row(query_id="q2"),
        _result_row(query_id="q3", human_correctness="1", human_completeness="2", human_relevance="3", human_clarity="1"),
    ]
    assert grade._next_ungraded_index(rows, 0) == 1
    assert grade._next_ungraded_index(rows, 2) == 1
    assert grade._next_ungraded_index(rows, 1) is None


def test_local_grading_ui_builds_markdown_columns_and_rubric(tmp_path):
    path = tmp_path / "benchmark-results.csv"
    row = _result_row()
    _write_results(path, benchmark_results.BENCHMARK_RESULT_COLUMNS, [row])

    app = grade.build_ui(path)
    config = app.get_config_file()
    labels = {
        component.get("props", {}).get("label")
        for component in config["components"]
    }
    markdown_values = [
        str(component.get("props", {}).get("value", ""))
        for component in config["components"]
    ]

    assert any("Question" in value for value in markdown_values)
    assert any("Ground Truth Answer" in value for value in markdown_values)
    assert any("Generated Answer" in value for value in markdown_values)
    assert {
        "Correctness",
        "Completeness",
        "Relevance",
        "Clarity",
        "Comments",
    } <= labels
    assert config["dependencies"]
    assert grade._build_parser().parse_args(["--results-csv", str(path)]).host == "127.0.0.1"


def test_grader_cli_allows_explicit_container_bind_address(tmp_path):
    args = grade._build_parser().parse_args([
        "--results-csv",
        str(tmp_path / "results.csv"),
        "--host",
        "0.0.0.0",
    ])

    assert args.host == "0.0.0.0"


def test_resizer_uses_live_boundaries_and_captured_pointer():
    assert "leftRect.right + rightRect.left" in grade._RESIZE_JS
    assert "handle.setPointerCapture(event.pointerId)" in grade._RESIZE_JS
    assert "handle.addEventListener(\"pointermove\", move)" in grade._RESIZE_JS
