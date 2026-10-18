"""Tests for CSV validation and sequential benchmark execution."""

import csv
import hashlib
import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))

from webui import benchmark, cli  # noqa: E402


def _write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


def test_example_csv_files_parse_questions_with_punctuation():
    example_dir = Path(__file__).resolve().parents[1] / "examples" / "benchmark"

    models = benchmark.load_models_csv(example_dir / "models.csv")
    queries = benchmark.load_queries_csv(example_dir / "questions.csv")

    assert models[0].provider == "openai"
    assert len(queries) == 2
    assert "," in queries[0].question
    assert ";" in queries[0].ground_truth


def test_answer_newlines_are_escaped_by_default_and_can_be_preserved(tmp_path):
    models_path = _write_csv(
        tmp_path / "models.csv",
        ["provider", "model"],
        [["openai", "model-a"]],
    )
    queries_path = _write_csv(
        tmp_path / "queries.csv",
        ["id", "question", "ground_truth", "citygml_dataset"],
        [["q1", "question", "truth", "dataset"]],
    )
    raw_answer = "first\r\nsecond\nthird\rfourth"

    class MultilineConsole(_RecordingConsole):
        def answer(self, question):
            return raw_answer

    parser = cli._build_parser()
    default_args = parser.parse_args([
        "benchmark",
        "--models-csv", str(models_path),
        "--queries-csv", str(queries_path),
        "--dataset", "dataset",
        "--output", str(tmp_path / "escaped.csv"),
    ])
    assert default_args.escape_answer_newlines is True

    for args, expected in (
        (default_args, r"first\nsecond\nthird\nfourth"),
        (
            parser.parse_args([
                "benchmark",
                "--models-csv", str(models_path),
                "--queries-csv", str(queries_path),
                "--dataset", "dataset",
                "--output", str(tmp_path / "preserved.csv"),
                "--no-escape-answer-newlines",
            ]),
            raw_answer,
        ),
    ):
        benchmark.run_benchmark(
            args,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            console_factory=MultilineConsole,
            assembler=lambda **_: "prompt",
        )

        with Path(args.output).open("r", encoding="utf-8", newline="") as stream:
            result = next(csv.DictReader(stream))
        assert result["generated_answer"] == expected


def test_model_csv_validation_and_optional_overrides(tmp_path):
    path = _write_csv(
        tmp_path / "models.csv",
        ["provider", "model", "base_url", "temperature", "thinking_level", "prompt_mode"],
        [
            ["OPENAI", "model-a", "https://example.test/v1", "0", "high", "full"],
            ["ollama", "model-b", "", "", "", ""],
        ],
    )

    models = benchmark.load_models_csv(path)

    assert len(models) == 2
    assert models[0].provider == "openai"
    assert models[0].temperature == 0.0
    assert models[0].thinking_level == "high"
    assert models[1].temperature is None
    assert models[1].prompt_mode is None


@pytest.mark.parametrize(
    ("headers", "rows", "message"),
    [
        (["provider"], [["openai"]], "missing required column"),
        (["provider", "model"], [["bad-provider", "m"]], "provider must be one of"),
        (["provider", "model", "temperature"], [["openai", "m", "2"]], "temperature must be between"),
        (["provider", "model", "base_url"], [["anthropic", "m", "https://host.test"]], "not supported for Anthropic"),
        (["provider", "model", "extra"], [["openai", "m", "x"]], "unsupported column"),
    ],
)
def test_model_csv_rejects_invalid_values(tmp_path, headers, rows, message):
    path = _write_csv(tmp_path / "models.csv", headers, rows)

    with pytest.raises(benchmark.BenchmarkInputError, match=message):
        benchmark.load_models_csv(path)


def test_query_csv_validates_required_fields_ids_and_groups(tmp_path):
    path = _write_csv(
        tmp_path / "queries.csv",
        ["id", "question", "ground_truth", "citygml_dataset", "group_number", "sequence_number"],
        [
            ["q1", "first", "answer", "dataset-a", "3", "2"],
            ["q2", "second", "answer", "dataset-a", "3", "1"],
        ],
    )

    queries = benchmark.load_queries_csv(path)

    assert [item.query_id for item in queries] == ["q1", "q2"]
    assert [item.sequence_number for item in queries] == [2, 1]
    assert [item.query_id for item in benchmark._work_units(queries)[0].queries] == ["q2", "q1"]

    duplicate_id = _write_csv(
        tmp_path / "duplicate.csv",
        ["id", "question", "ground_truth", "citygml_dataset"],
        [["same", "one", "truth", "dataset"], ["same", "two", "truth", "dataset"]],
    )
    with pytest.raises(benchmark.BenchmarkInputError, match="duplicate query id"):
        benchmark.load_queries_csv(duplicate_id)

    mixed_dataset = _write_csv(
        tmp_path / "mixed-dataset.csv",
        ["id", "question", "ground_truth", "citygml_dataset", "group_number", "sequence_number"],
        [
            ["q1", "one", "truth", "dataset-a", "1", "1"],
            ["q2", "two", "truth", "dataset-b", "1", "2"],
        ],
    )
    with pytest.raises(benchmark.BenchmarkInputError, match="different citygml_dataset"):
        benchmark.load_queries_csv(mixed_dataset)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([["q", "ask", "truth", "d", "1", ""]], "sequence_number is required"),
        ([["q", "ask", "truth", "d", "", "1"]], "requires a group_number"),
        (
            [["q1", "one", "truth", "d", "1", "1"], ["q2", "two", "truth", "d", "1", "1"]],
            "duplicate sequence_number",
        ),
    ],
)
def test_query_csv_rejects_invalid_group_sequence(tmp_path, rows, message):
    path = _write_csv(
        tmp_path / "queries.csv",
        ["id", "question", "ground_truth", "citygml_dataset", "group_number", "sequence_number"],
        rows,
    )

    with pytest.raises(benchmark.BenchmarkInputError, match=message):
        benchmark.load_queries_csv(path)


def test_database_env_file_supports_dotenv_and_database_url(tmp_path):
    path = tmp_path / "benchmark.env"
    path.write_text(
        "DATABASE_URL=postgresql://city-user:p%40ss@db.example:5544/city-model\n"
        "CITYDB_SCHEMA=urban\n",
        encoding="utf-8",
    )

    values = benchmark._database_env_values(path)

    assert values == {
        "CITYDB_HOST": "db.example",
        "CITYDB_PORT": "5544",
        "CITYDB_NAME": "city-model",
        "CITYDB_USER": "city-user",
        "CITYDB_PASSWORD": "p@ss",
        "CITYDB_SCHEMA": "urban",
    }

    bad_port = tmp_path / "bad-port.env"
    bad_port.write_text("CITYDB_PORT=invalid\n", encoding="utf-8")
    with pytest.raises(benchmark.BenchmarkInputError, match="CITYDB_PORT must be an integer"):
        benchmark._database_env_values(bad_port)


def test_result_endpoint_and_errors_redact_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-openai-key")

    safe_endpoint = benchmark._safe_base_url(
        "https://user:password@example.test/v1?api_key=secret&region=eu"
    )
    safe_error = benchmark._safe_error(
        RuntimeError(
            "request failed https://user:password@example.test/v1"
            "?token=secret-openai-key"
        )
    )

    assert safe_endpoint == "https://example.test/v1?api_key=%5BREDACTED%5D&region=eu"
    assert "user" not in safe_error
    assert "password" not in safe_error
    assert "secret-openai-key" not in safe_error


def test_model_endpoint_override_is_applied_and_restored(monkeypatch):
    model = benchmark.ModelConfig(
        row_number=2,
        provider="openai",
        model="model-a",
        base_url="https://custom.example/v1",
        temperature=None,
        thinking_level=None,
        prompt_mode=None,
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "https://default.example/v1")

    with benchmark._model_endpoint(model, "https://run-default.example/v1") as endpoint:
        assert endpoint == "https://custom.example/v1"
        assert os.environ["OPENAI_BASE_URL"] == "https://custom.example/v1"

    assert os.environ["OPENAI_BASE_URL"] == "https://default.example/v1"


def test_cli_settings_override_csv_and_csv_settings_are_defaults(tmp_path):
    model_path = _write_csv(
        tmp_path / "models.csv",
        ["provider", "model", "temperature", "thinking_level", "prompt_mode"],
        [["openai", "model-a", "0.2", "low", "compact"]],
    )
    model = benchmark.load_models_csv(model_path)[0]
    parser = cli._build_parser()
    base_args = [
        "benchmark",
        "--models-csv", str(model_path),
        "--queries-csv", str(tmp_path / "queries.csv"),
        "--dataset", "dataset",
        "--output", str(tmp_path / "results.csv"),
    ]

    csv_settings = benchmark._effective_settings(model, parser.parse_args(base_args))
    assert csv_settings.temperature == 0.2
    assert csv_settings.enable_thinking == "low"
    assert csv_settings.prompt_mode == "compact"

    cli_settings = benchmark._effective_settings(
        model,
        parser.parse_args([
            *base_args,
            "--temperature", "0.7",
            "--thinking", "high",
            "--prompt-mode", "full",
        ]),
    )
    assert cli_settings.temperature == 0.7
    assert cli_settings.enable_thinking == "high"
    assert cli_settings.prompt_mode == "full"


class _RecordingConsole:
    instances = []

    def __init__(self, **kwargs):
        self.settings = kwargs["settings"]
        self.stdout = kwargs["stdout"]
        self.stderr = kwargs["stderr"]
        self.history = []
        self.last_context_tokens = 123
        self.last_context_limit = 4096
        self.last_trimmed_turns = 1
        self.last_elapsed_seconds = 0.25
        self.instances.append(self)

    def answer(self, question):
        self.history.append(question)
        if question == "fail":
            raise RuntimeError("provider call failed")
        return f"answer:{question}:prior={len(self.history) - 1}"


def test_runner_filters_dataset_orders_groups_repeats_fresh_and_flushes(tmp_path):
    models_path = _write_csv(
        tmp_path / "models.csv",
        ["provider", "model", "temperature", "thinking_level", "prompt_mode"],
        [
            ["openai", "model-a", "", "", ""],
            ["ollama", "model-b", "0.2", "low", "compact"],
        ],
    )
    queries_path = _write_csv(
        tmp_path / "queries.csv",
        ["id", "question", "ground_truth", "citygml_dataset", "category", "group_number", "sequence_number"],
        [
            ["g2", "second", "truth2", "selected", "grouped", "9", "2"],
            ["standalone", "solo", "truth-solo", "selected", "", "", ""],
            ["g1", "first", "truth1", "selected", "grouped", "9", "1"],
            ["other", "ignore", "truth", "different", "", "", ""],
        ],
    )
    output_path = tmp_path / "nested" / "results.csv"
    args = cli._build_parser().parse_args([
        "benchmark",
        "--models-csv", str(models_path),
        "--queries-csv", str(queries_path),
        "--dataset", "selected",
        "--repetitions", "2",
        "--output", str(output_path),
        "--temperature", "0.7",
        "--thinking", "high",
        "--prompt-mode", "full",
    ])
    _RecordingConsole.instances.clear()
    assemble_calls = []

    def fake_assemble(*, compact, stderr):
        assert stderr is not None
        assemble_calls.append(compact)
        return "system prompt"

    stdout = io.StringIO()
    stderr = io.StringIO()
    result = benchmark.run_benchmark(
        args,
        stdout=stdout,
        stderr=stderr,
        console_factory=_RecordingConsole,
        assembler=fake_assemble,
    )

    with output_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert result == 0
    assert [row["query_id"] for row in rows] == [
        "g1", "g2", "standalone",
        "g1", "g2", "standalone",
    ] * 2
    assert len({row["run_id"] for row in rows}) == 1
    assert all(row["query_file_sha256"] for row in rows)
    assert rows[0]["query_file_sha256"] == hashlib.sha256(
        queries_path.read_bytes()
    ).hexdigest()
    assert all(row["context_tokens"] == "123" for row in rows)
    assert all(row["context_limit"] == "4096" for row in rows)
    assert all(row["context_trimmed_turns"] == "1" for row in rows)
    assert all(row["status"] == "completed" for row in rows)
    assert rows[0]["conversation_id"] == rows[1]["conversation_id"]
    assert rows[0]["conversation_id"] != rows[3]["conversation_id"]
    assert rows[0]["generated_answer"].endswith("prior=0")
    assert rows[1]["generated_answer"].endswith("prior=1")
    assert rows[2]["generated_answer"].endswith("prior=0")
    assert rows[3]["generated_answer"].endswith("prior=0")
    assert rows[0]["temperature"] == "0.7"
    assert rows[6]["temperature"] == "0.7"
    assert rows[6]["thinking_level"] == "high"
    assert rows[6]["prompt_mode"] == "full"
    assert assemble_calls == [False, False]
    assert len(_RecordingConsole.instances) == 8
    assert "12/12 turn(s) recorded" in stdout.getvalue()
    assert "query=other" not in stderr.getvalue()
    assert "query=standalone repetition=2\n\n\n\nPreparing ollama/model-b" in stderr.getvalue()
    stdout_text = stdout.getvalue()
    assert (
        "Provider: openai | Model: model-a (models.csv line 2 of 3) | Prompt: full"
    ) in stdout_text
    assert "Group: 9 | Query: 1 of 2 | Repetition: 1 of 2" in stdout_text
    assert "Group: 9 | Query: 2 of 2 | Repetition: 1 of 2" in stdout_text
    assert "Group: standalone (standalone) | Query: 1 of 1 | Repetition: 1 of 2" in stdout_text
    assert "User query: first\nAnswer:\nanswer:first:prior=0" in stdout_text
    assert (
        "Answer:\nanswer:solo:prior=0\n\n\n\n"
        "Provider: ollama | Model: model-b (models.csv line 3 of 3) | Prompt: full"
    ) in stdout_text
    assert _RecordingConsole.instances[0].stdout is stdout


def test_runner_records_failures_without_discarding_other_answers(tmp_path):
    models_path = _write_csv(
        tmp_path / "models.csv",
        ["provider", "model"],
        [["openai", "model-a"]],
    )
    queries_path = _write_csv(
        tmp_path / "queries.csv",
        ["id", "question", "ground_truth", "citygml_dataset"],
        [["good", "hello", "hi", "dataset"], ["bad", "fail", "truth", "dataset"]],
    )
    output_path = tmp_path / "results.csv"
    args = cli._build_parser().parse_args([
        "benchmark",
        "--models-csv", str(models_path),
        "--queries-csv", str(queries_path),
        "--dataset", "dataset",
        "--output", str(output_path),
    ])

    benchmark.run_benchmark(
        args,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        console_factory=_RecordingConsole,
        assembler=lambda **_: "prompt",
    )

    with output_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["status"] == "completed"
    assert rows[0]["generated_answer"] == "answer:hello:prior=0"
    assert rows[1]["status"] == "failed"
    assert rows[1]["error"] == "RuntimeError: provider call failed"
    assert rows[1]["generated_answer"] == ""


def test_runner_applies_db_override_and_restores_environment(tmp_path, monkeypatch):
    models_path = _write_csv(
        tmp_path / "models.csv",
        ["provider", "model"],
        [["openai", "model-a"]],
    )
    queries_path = _write_csv(
        tmp_path / "queries.csv",
        ["id", "question", "ground_truth", "citygml_dataset"],
        [["q1", "hello", "truth", "dataset"]],
    )
    env_path = tmp_path / "db.env"
    env_path.write_text("CITYDB_HOST=benchmark-db\n", encoding="utf-8")
    output_path = tmp_path / "results.csv"
    args = cli._build_parser().parse_args([
        "benchmark",
        "--models-csv", str(models_path),
        "--queries-csv", str(queries_path),
        "--dataset", "dataset",
        "--output", str(output_path),
        "--env-file", str(env_path),
    ])
    monkeypatch.setenv("CITYDB_HOST", "default-db")
    seen_hosts = []

    def assemble(*, compact, stderr):
        assert not compact
        assert stderr is not None
        seen_hosts.append(os.environ["CITYDB_HOST"])
        return "prompt"

    benchmark.run_benchmark(
        args,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        console_factory=_RecordingConsole,
        assembler=assemble,
    )

    assert seen_hosts == ["benchmark-db"]
    assert os.environ["CITYDB_HOST"] == "default-db"
