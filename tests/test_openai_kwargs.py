"""Regression tests for OpenAI-compatible reasoning_effort forwarding."""

import os
import sys

import litellm

for _cand in (
    os.path.join(os.path.dirname(__file__), "..", "production"),
    "/app",
):
    if os.path.isdir(os.path.join(_cand, "webui")):
        sys.path.insert(0, _cand)
        break

from webui.llm_utils import (  # noqa: E402
    _is_reasoning_effort_rejection,
    _litellm_kwargs,
    safe_completion,
)


def test_openai_reasoning_effort_mapping() -> None:
    expected = {
        False: "none",
        "off": "none",
        "low": "low",
        "medium": "medium",
        "high": "high",
        True: "high",
    }
    for thinking, effort in expected.items():
        kwargs = _litellm_kwargs("openai", "gpt-oss:20b", 0.4, enable_thinking=thinking)
        assert kwargs["temperature"] == 0.4
        assert kwargs["reasoning_effort"] == effort


def test_openai_reasoning_effort_can_be_omitted() -> None:
    kwargs = _litellm_kwargs("openai", "gpt-oss:20b", 0.4, enable_thinking=None)
    assert "reasoning_effort" not in kwargs


def test_temperature_can_be_omitted() -> None:
    kwargs = _litellm_kwargs("openai", "gpt-4o", None)
    assert "temperature" not in kwargs


def test_temperature_is_preserved_when_explicit() -> None:
    kwargs = _litellm_kwargs("openai", "gpt-4o", 0.65)
    assert kwargs["temperature"] == 0.65


def test_reasoning_effort_is_openai_only() -> None:
    assert "reasoning_effort" not in _litellm_kwargs(
        "anthropic", "claude-sonnet", 0.4, enable_thinking="high"
    )
    assert "reasoning_effort" not in _litellm_kwargs(
        "ollama", "qwen3:8b", 0.4, enable_thinking="high"
    )


def test_reasoning_effort_rejection_detection() -> None:
    assert _is_reasoning_effort_rejection(
        ValueError("Unsupported parameter: reasoning_effort")
    )
    assert _is_reasoning_effort_rejection(
        ValueError("unknown field 'reasoning effort'")
    )
    assert not _is_reasoning_effort_rejection(
        ValueError("reasoning_effort caused a timeout")
    )
    assert not _is_reasoning_effort_rejection(
        ValueError("invalid model name")
    )


def test_rejected_reasoning_effort_retries_without_it() -> None:
    calls = []
    original_completion = litellm.completion

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise litellm.BadRequestError(
                "Unsupported parameter: reasoning_effort",
                model=kwargs["model"],
                llm_provider="openai",
            )
        return {"ok": True}

    litellm.completion = fake_completion
    try:
        kwargs = _litellm_kwargs(
            "openai", "fallback-test-model", 0.4,
            enable_thinking="medium",
        )
        result = safe_completion(kwargs, messages=[])
    finally:
        litellm.completion = original_completion

    assert result == {"ok": True}
    assert calls[0]["reasoning_effort"] == "medium"
    assert "reasoning_effort" not in calls[1]


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
