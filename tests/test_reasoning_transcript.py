"""Tests for _build_reasoning_transcript (provider-agnostic event → transcript).

Covers both reasoning event styles:
- Ollama: live ``thinking_token`` stream
- OpenAI-compatible: complete ``thinking`` blobs per LLM call
"""

import os
import sys

for _cand in (
    os.path.join(os.path.dirname(__file__), "..", "production"),
    "/app",
):
    if os.path.isdir(os.path.join(_cand, "webui")):
        sys.path.insert(0, _cand)
        break

from webui.app import _build_reasoning_transcript  # noqa: E402


def test_ollama_thinking_token_stream() -> None:
    events = [
        ("thinking_token", "I need to "),
        ("thinking_token", "join feature with property."),
        ("tool_call", {"tool": "run_query", "args": {"sql": "SELECT 1"}, "iteration": 0}),
        ("tool_result", {"row_count": 5, "error": None, "iteration": 0}),
        ("thinking_token", "Now I know the answer."),
    ]
    transcript = _build_reasoning_transcript(events)
    assert "I need to join feature with property." in transcript
    assert "[Action] run_query: SELECT 1" in transcript
    assert "[Observation] 5 row(s) returned" in transcript
    assert "Now I know the answer." in transcript


def test_openai_thinking_blobs() -> None:
    events = [
        ("thinking", "Reasoning block one: pick the right table."),
        ("tool_call", {"tool": "run_query", "args": {"sql": "SELECT 2"}, "iteration": 0}),
        ("tool_result", {"row_count": 0, "error": None, "iteration": 0}),
        ("thinking", "Zero rows — switching to a different approach."),
        ("tool_call", {"tool": "run_query", "args": {"sql": "SELECT 3"}, "iteration": 1}),
        ("tool_result", {"row_count": 42, "error": None, "iteration": 1}),
    ]
    transcript = _build_reasoning_transcript(events)
    assert "Reasoning block one: pick the right table." in transcript
    assert "[Action] run_query: SELECT 2" in transcript
    assert "[Observation] 0 row(s) returned" in transcript
    assert "Zero rows — switching to a different approach." in transcript
    assert "[Action] run_query: SELECT 3" in transcript
    assert "[Observation] 42 row(s) returned" in transcript


def test_stream_ordering_with_partial_before_blob() -> None:
    """A partial token stream flushed before a thinking blob keeps order."""
    events = [
        ("thinking_token", "partial thought "),
        ("thinking", "complete thought"),
        ("tool_call", {"tool": "run_query", "args": {"sql": "SELECT 4"}, "iteration": 0}),
        ("tool_result", {"row_count": 1, "error": None, "iteration": 0}),
    ]
    transcript = _build_reasoning_transcript(events)
    # The flushed partial must appear before the blob, as separate entries.
    assert transcript.index("partial thought") < transcript.index("complete thought")
    assert "[Action] run_query: SELECT 4" in transcript


def test_error_observation() -> None:
    events = [
        ("tool_call", {"tool": "run_query", "args": {"sql": "SELECT 5"}, "iteration": 0}),
        ("tool_result", {"row_count": 0, "error": "relation does not exist", "iteration": 0}),
    ]
    transcript = _build_reasoning_transcript(events)
    assert "[Observation] Error: relation does not exist" in transcript


def test_no_events_yields_empty() -> None:
    assert _build_reasoning_transcript([]) == ""


def test_non_reasoning_model_yields_only_skeleton() -> None:
    """OpenAI model without reasoning: transcript is the Action/Observation
    skeleton only — replay must degrade to that, not crash."""
    events = [
        ("tool_call", {"tool": "run_query", "args": {"sql": "SELECT 6"}, "iteration": 0}),
        ("tool_result", {"row_count": 7, "error": None, "iteration": 0}),
    ]
    transcript = _build_reasoning_transcript(events)
    assert transcript == "[Action] run_query: SELECT 6\n[Observation] 7 row(s) returned"
