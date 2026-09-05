"""Tests for display-only answer duration formatting."""

import os
import re
import sys

for _cand in (
    os.path.join(os.path.dirname(__file__), "..", "production"),
    "/app",
):
    if os.path.isdir(os.path.join(_cand, "webui")):
        sys.path.insert(0, _cand)
        break

from webui.app import _history_with_duration  # noqa: E402


def test_duration_is_appended_to_display_copy_only() -> None:
    history = [["Question", "Answer"]]
    display = _history_with_duration(history, 0.0)

    assert history == [["Question", "Answer"]]
    assert display is not history
    assert display[0] is not history[0]
    assert display[0][0] == "Question"
    assert re.search(r"Time to answer the question \d+\.\d{3} seconds", display[0][1])


def test_empty_history_is_safe() -> None:
    assert _history_with_duration([], 0.0) == []


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
