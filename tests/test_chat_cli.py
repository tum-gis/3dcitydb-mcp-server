"""Tests for the terminal chat interface."""

import io
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))

from webui import cli  # noqa: E402
from webui.chat_runtime import ChatTurnSettings, PreparedChatTurn  # noqa: E402


def test_prompt_assembly_displays_progress_and_removes_progress_file(monkeypatch):
    progress_paths = []

    def fake_assemble(*, compact, progress_path, stderr):
        progress_paths.append(progress_path)
        records = [
            {"step": 1, "total": 1, "label": "Loading schema", "state": "start"},
            {"step": 2, "total": 1, "label": "Loading schema", "state": "done"},
        ]
        with open(progress_path, "a", encoding="utf-8") as progress:
            progress.write(json.dumps(records[0]) + "\n")
            progress.flush()
            time.sleep(0.25)
            progress.write(json.dumps(records[1]) + "\n")
        assert compact is True
        return "database schema"

    monkeypatch.setattr(cli, "assemble_system_prompt_sync", fake_assemble)
    stdout = io.StringIO()
    stderr = io.StringIO()

    prompt = cli._assemble_prompt_with_progress(stdout, stderr=stderr, compact=True)

    assert prompt == "database schema"
    assert "Assembling system prompt" in stdout.getvalue()
    step_lines = [
        line for line in stdout.getvalue().splitlines()
        if line.startswith("  Step ")
    ]
    assert len(step_lines) == 1
    assert step_lines[0].startswith("  Step 1/1: Loading schema ")
    assert "- finished in " in step_lines[0]
    assert step_lines[0].endswith(" s")
    assert float(step_lines[0].partition("- finished in ")[2][:-2]) >= 0
    assert "System prompt assembled after 1 steps." in stdout.getvalue()
    assert not Path(progress_paths[0]).exists()


def test_prompt_assembly_reports_error_and_removes_progress_file(monkeypatch):
    progress_paths = []

    def fail_assemble(**kwargs):
        progress_paths.append(kwargs["progress_path"])
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(cli, "assemble_system_prompt_sync", fail_assemble)
    stdout = io.StringIO()
    stderr = io.StringIO()

    try:
        cli._assemble_prompt_with_progress(stdout, stderr=stderr)
    except RuntimeError as error:
        assert str(error) == "database unavailable"
    else:
        raise AssertionError("Expected prompt assembly to fail")

    assert not Path(progress_paths[0]).exists()
    assert "System prompt assembly failed." in stderr.getvalue()


def test_activity_output_labels_reasoning_and_tool_events():
    activity = cli._format_activity([
        ("thinking_token", "I will query."),
        ("tool_call", {"args": {"sql": "SELECT 1"}}),
        ("tool_result", {"row_count": 1, "execution_time_ms": 4}),
    ])

    assert "Provider-emitted reasoning:\nI will query." in activity
    assert "Tool call (run_query)" in activity
    assert "Tool result: 1 row(s), 4 ms" in activity
    assert "SELECT 1" in activity


def test_chat_console_uses_shared_runtime_and_keeps_answer_separate(monkeypatch):
    prepared_calls = []

    def fake_prepare(question, history, system_prompt, settings, **kwargs):
        prepared_calls.append((question, history, kwargs))
        return PreparedChatTurn(
            messages=[{"role": "system", "content": system_prompt}],
            effective_compact=False,
            prompt_mode_label="full",
            context_limit=1000,
            estimated_tokens=150,
            dropped_messages=0,
        )

    def fake_stream(settings, messages, tool_executor):
        tool_executor("SELECT objectid FROM feature")
        yield "thinking_token", "Reasoning only."
        yield "tool_call", {
            "args": {"sql": "SELECT objectid FROM feature"},
            "iteration": 0,
        }
        yield "tool_result", {
            "row_count": 1,
            "all_rows": [{"objectid": "building-1"}],
            "preview_rows": [{"objectid": "building-1"}],
            "execution_time_ms": 7,
        }
        yield "final", "Assistant answer."

    monkeypatch.setattr(cli, "prepare_chat_turn", fake_prepare)
    monkeypatch.setattr(cli, "stream_chat_turn", fake_stream)
    monkeypatch.setattr(cli, "run_tool_sync", lambda *args, **_kwargs: "tool result")
    monkeypatch.setattr(
        cli,
        "summarize_story_openai",
        lambda *args: ("successful query path", ""),
    )
    stderr = io.StringIO()
    stdout = io.StringIO()
    console = cli.ChatConsole(
        settings=ChatTurnSettings(provider="openai", model="test"),
        system_prompt="database schema",
        reasoning_replay=True,
        show_reasoning=True,
        stderr=stderr,
        stdout=stdout,
    )

    answer = console.answer("How many buildings?")

    assert answer == "Assistant answer."
    assert console.history == [["How many buildings?", "Assistant answer."]]
    assert console.reasoning_history == ["Reasoning only.\n[Action] run_query: SELECT objectid FROM feature\n[Observation] 1 row(s) returned"]
    assert console.story_history == ["successful query path"]
    assert console.tool_cache is not None
    assert "Reasoning / Agent activity — not the final answer" in stderr.getvalue()
    assert "Provider-emitted reasoning:\nReasoning only." in stderr.getvalue()
    assert "Assistant answer." not in stderr.getvalue()
    assert "Context estimate:" in stdout.getvalue()
    assert prepared_calls[0][2]["reasoning_replay"] is True


def test_piped_cli_writes_answer_only_to_stdout(monkeypatch):
    monkeypatch.setattr(cli, "detect_default_provider", lambda: "openai")
    monkeypatch.setattr(cli, "default_model", lambda _: "test-model")
    monkeypatch.setattr(cli, "assemble_system_prompt_sync", lambda **_: "schema prompt")
    def fake_stream(*_args, **_kwargs):
        print("[backend] internal progress")
        yield "final", "The final answer."

    monkeypatch.setattr(cli, "stream_chat_turn", fake_stream)
    args = cli._build_parser().parse_args(["chat", "--no-keep-story"])
    stdout = io.StringIO()
    stderr = io.StringIO()

    result = cli.run_chat(
        args,
        stdin=io.StringIO("What is in the database?\n"),
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 0
    assert "Provider: openai" in stdout.getvalue()
    assert "Assembling system prompt" in stdout.getvalue()
    assert "Context estimate:" in stdout.getvalue()
    assert "The final answer.\n" in stdout.getvalue()
    assert "Context estimate:" not in stderr.getvalue()
    assert "[backend] internal progress" in stderr.getvalue()
    assert "The final answer." not in stderr.getvalue()


def test_quiet_stderr_suppresses_cli_and_logging_output(monkeypatch):
    original_disable = logging.root.manager.disable
    original_stderr = sys.stderr

    def fake_run_chat(_args, *, stdin, stdout, stderr):
        assert stderr is not original_stderr
        print("diagnostic output", file=stderr)
        logging.warning("logging output")
        return 0

    monkeypatch.setattr(cli, "run_chat", fake_run_chat)
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    result = cli.main(["chat", "--quiet-stderr"])

    assert result == 0
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""
    assert logging.root.manager.disable == original_disable


def test_non_tty_no_question_explains_docker_tty_requirement(monkeypatch):
    monkeypatch.setattr(cli, "detect_default_provider", lambda: "openai")
    monkeypatch.setattr(cli, "default_model", lambda _: "test-model")
    monkeypatch.setattr(
        cli,
        "assemble_system_prompt_sync",
        lambda **_: (_ for _ in ()).throw(AssertionError("prompt assembled unnecessarily")),
    )
    args = cli._build_parser().parse_args(["chat", "--no-keep-story"])
    stderr = io.StringIO()

    result = cli.run_chat(args, stdin=io.StringIO(""), stderr=stderr)

    assert result == 2
    assert "docker exec -it" in stderr.getvalue()


def test_interactive_cli_handles_new_and_bye_commands(monkeypatch):
    monkeypatch.setattr(cli, "detect_default_provider", lambda: "openai")
    monkeypatch.setattr(cli, "default_model", lambda _: "test-model")
    monkeypatch.setattr(cli, "assemble_system_prompt_sync", lambda **_: "schema prompt")
    answered = []

    class InteractiveInput(io.StringIO):
        def isatty(self):
            return True

    class FakeConsole:
        def __init__(self, **_kwargs):
            self.reset_count = 0

        def answer(self, question):
            answered.append(question)
            return f"Answer to {question}"

        def reset(self):
            self.reset_count += 1

    monkeypatch.setattr(cli, "ChatConsole", FakeConsole)
    args = cli._build_parser().parse_args(["chat", "--no-keep-story"])
    stdout = io.StringIO()
    stderr = io.StringIO()

    result = cli.run_chat(
        args,
        stdin=InteractiveInput("first question\n/new\nsecond question\n/bye\n"),
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 0
    assert answered == ["first question", "second question"]
    assert "Answer to first question" in stdout.getvalue()
    assert "Answer to second question" in stdout.getvalue()
    assert "Enter /new" in stdout.getvalue()
    assert "Conversation cleared." in stdout.getvalue()
    assert "You:" in stdout.getvalue()
    assert "/bye" not in stderr.getvalue()


def test_cli_settings_match_configurable_chat_options(monkeypatch):
    parser = cli._build_parser()
    args = parser.parse_args([
        "chat",
        "--provider", "ollama",
        "--model", "local-test",
        "--temperature", "0",
        "--thinking", "high",
        "--prompt-mode", "compact",
        "--num-ctx", "65536",
        "--no-keep-story",
        "--include-reasoning",
        "--show-reasoning",
    ])

    settings = cli._resolve_settings(args, parser)

    assert settings == ChatTurnSettings(
        provider="ollama",
        model="local-test",
        temperature=0,
        enable_thinking="high",
        prompt_mode="compact",
        num_ctx=65536,
    )
    assert args.keep_story is False
    assert args.include_reasoning is True
    assert args.show_reasoning is True
