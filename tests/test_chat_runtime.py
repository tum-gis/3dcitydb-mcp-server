"""Tests for the Gradio-independent conversation runtime."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))

from webui import chat_runtime  # noqa: E402
from webui.chat_runtime import ChatTurnSettings  # noqa: E402


def test_prepare_chat_turn_replays_prior_context_and_injects_notes_in_order(
    monkeypatch,
):
    monkeypatch.setattr(chat_runtime, "_get_provider_ctx_limit", lambda *_: 4096)
    monkeypatch.setattr(chat_runtime, "_estimate_tokens", lambda _: 123)
    # Other test modules load a local .env; keep its prompt additions out of this one.
    monkeypatch.setattr(chat_runtime, "extra_prompt_blocks", lambda: "")
    history = [["first question", "There are 2 buildings."], ["follow-up", "Found 2."]]

    prepared = chat_runtime.prepare_chat_turn(
        "new question",
        history,
        "database prompt",
        ChatTurnSettings(provider="openai", model="test-model"),
        leading_system_notes=["selection", "cache"],
    )

    assert prepared.messages == [
        {"role": "system", "content": chat_runtime.CHAT_INSTRUCTIONS + "\n\ndatabase prompt"},
        {"role": "system", "content": "selection"},
        {"role": "system", "content": "cache"},
        {"role": "user", "content": "first question"},
        {"role": "user", "content": "follow-up"},
        {"role": "assistant", "content": "Found 2."},
        {"role": "user", "content": "new question"},
    ]
    assert prepared.context_limit == 4096
    assert prepared.estimated_tokens == 123


def test_build_messages_replays_reasoning_and_distilled_story():
    messages = chat_runtime.build_messages(
        "next",
        [["question", "answer"]],
        "prompt",
        reasoning_history=["thoughts"],
        story_history=["successful path"],
        reasoning_replay=True,
        add_story=True,
    )

    assert messages[2] == {
        "role": "assistant",
        "content": "[Reasoning]\nthoughts\n\n[Answer]\nanswer",
    }
    assert messages[3]["role"] == "system"
    assert messages[3]["content"].endswith("successful path")


def test_build_reasoning_transcript_preserves_action_observation_order():
    transcript = chat_runtime.build_reasoning_transcript([
        ("thinking_token", "Plan "),
        ("thinking_token", "query"),
        ("tool_call", {"args": {"sql": "SELECT 1"}}),
        ("tool_result", {"row_count": 1}),
    ])

    assert transcript == (
        "Plan query\n[Action] run_query: SELECT 1\n"
        "[Observation] 1 row(s) returned"
    )


def test_stream_chat_turn_forwards_provider_settings(monkeypatch):
    calls = []

    def fake_stream(*args, **kwargs):
        calls.append((args, kwargs))
        yield "content", "answer"

    monkeypatch.setattr("webui.backends.stream", fake_stream)
    settings = ChatTurnSettings(
        provider="ollama",
        model="local-model",
        temperature=0.2,
        enable_thinking="high",
        num_ctx=8192,
    )

    assert list(chat_runtime.stream_chat_turn(settings, [], lambda _: "")) == [
        ("content", "answer")
    ]
    args, kwargs = calls[0]
    assert args[:3] == ("ollama", "local-model", 0.2)
    assert kwargs["enable_thinking"] == "high"
    assert kwargs["num_ctx"] == 8192


def test_trim_messages_preserves_system_and_newest_context(monkeypatch):
    monkeypatch.setattr(chat_runtime, "_MAX_CONTEXT_CHARS", 10)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "new"},
    ]

    kept, dropped = chat_runtime.trim_messages(messages)

    assert kept == [messages[0], messages[3]]
    assert dropped == 2


def test_tool_cache_is_bounded_and_formatted_for_followups(monkeypatch):
    monkeypatch.setattr(chat_runtime, "_TOOL_CACHE_ROW_CAP", 2)
    cache = chat_runtime.build_tool_cache(
        "SELECT objectid FROM feature",
        [{"objectid": "a"}, {"objectid": "b"}, {"objectid": "c"}],
        3,
    )

    assert cache is not None
    assert cache["truncated"] is True
    assert len(cache["rows"]) == 2
    assert chat_runtime.cache_is_fresh(cache)
    note = chat_runtime.format_tool_cache_note(cache)
    assert "SELECT objectid FROM feature" in note
    assert "Returned 3 row(s) (showing first 2)" in note


def test_tool_cache_rejects_empty_result():
    assert chat_runtime.build_tool_cache("SELECT 1", [], 0) is None
