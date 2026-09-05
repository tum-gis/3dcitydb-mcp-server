"""Tests for reasoning fields returned by OpenAI-compatible chat responses."""

import os
import sys
from types import SimpleNamespace

for _cand in (
    os.path.join(os.path.dirname(__file__), "..", "production"),
    "/app",
):
    if os.path.isdir(os.path.join(_cand, "webui")):
        sys.path.insert(0, _cand)
        break

from webui.backends.cloud import _extract_reasoning, _merge_reasoning  # noqa: E402


def test_reasoning_content_field() -> None:
    message = SimpleNamespace(reasoning_content="Inspecting the schema first.")
    assert _extract_reasoning(message) == "Inspecting the schema first."


def test_reasoning_details_are_normalized() -> None:
    message = SimpleNamespace(
        reasoning_details=[
            {"type": "text", "text": "First step."},
            {"type": "text", "content": "Second step."},
        ]
    )
    assert _extract_reasoning(message) == "First step.\nSecond step."


def test_additional_kwargs_reasoning_is_supported() -> None:
    message = SimpleNamespace(
        additional_kwargs={"reasoning": "Reasoning from provider metadata."}
    )
    assert _extract_reasoning(message) == "Reasoning from provider metadata."


def test_duplicate_reasoning_is_not_repeated() -> None:
    assert _merge_reasoning("same", "same", "next") == "same\n\nnext"


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
