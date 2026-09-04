"""Unit tests for the empirical model-class registry (plan step e).

Run from the repo root (host):
    python tests/test_model_profiles.py
or inside the agent image:
    docker run --rm -v <repo>:/repo citydb-mcp-agent:local python /repo/tests/test_model_profiles.py

Contract under test:
  * all 10 probed model strings classify to the expected class
  * unknown models map to `unknown` with no warning
  * per-class warning / recommendation flags are as specced
  * _resolve_compact uses the class default when prompt mode is "auto"
"""

import os
import sys

# Host: repo root layout → production/ contains the webui package.
# Container: the webui package is installed at /app.
for _cand in (
    os.path.join(os.path.dirname(__file__), "..", "production"),
    "/app",
):
    if os.path.isdir(os.path.join(_cand, "webui")):
        sys.path.insert(0, _cand)
        break

from webui.model_profiles import profile_for_model  # noqa: E402

# The 10 models from the probe matrix → expected empirical class.
PROBED = {
    "qwen3.8:27b": "works",
    "qwen3.6:35b-a3b-q4_K_M": "works",
    "ministral-3:14b": "works",
    "nemotron-3-nano:latest": "works",
    "phi4:14b-q4_K_M": "works",
    "gemma4:12b-it-q8_0": "wrong-sql",
    "gemma4:26b-a4b-it-q4_K_M": "thinking-then-empty",
    "gemma4:31b-it-q4_K_M": "thinking-then-empty",
    "granite4.2:30b-q4_K_M": "sentence-as-tool",
    # Moved from "reasoning-leak" to "wrong-sql" after the parser fix made
    # gpt-oss reasoning dumps recoverable; remaining risk is the wrong
    # SQL metric (see docs/local-model-probing.md).
    "gpt-oss:20b": "wrong-sql",
}


def test_all_probed_models_classify() -> None:
    for model, expected in PROBED.items():
        prof = profile_for_model(model)
        assert prof.model_class == expected, (
            f"{model}: expected {expected}, got {prof.model_class}"
        )


def test_unknown_models() -> None:
    for model in ("totally/unknown:0", "llama3.1:8b", "mistral:7b", ""):
        prof = profile_for_model(model)
        assert prof.model_class == "unknown", f"{model!r}: expected unknown"
        assert prof.warning == "", f"{model!r}: unknown must have no warning"
        assert prof.default_prompt_mode == "auto"


def test_class_flags() -> None:
    cases = {
        # (class, expected_recommended, expected_prompt_mode, warn_required)
        "works": (True, "auto", False),
        "sentence-as-tool": (True, "auto", True),      # informational note
        "wrong-sql": (True, "full", True),
        "thinking-then-empty": (False, "auto", True),
        "reasoning-leak": (False, "auto", True),
    }
    for model, cls in PROBED.items():
        rec, mode, warn_req = cases[cls]
        prof = profile_for_model(model)
        assert prof.recommended is rec, f"{model}: recommended flag wrong"
        assert prof.default_prompt_mode == mode, f"{model}: prompt mode wrong"
        assert (prof.warning != "") is warn_req, f"{model}: warning presence wrong"


def test_resolve_compact_uses_class_default() -> None:
    """app._resolve_compact: auto prompt mode must follow the model class."""
    try:
        from webui.app import _resolve_compact
    except Exception as e:  # gradio or heavy deps unavailable on host
        print(f"SKIP test_resolve_compact_uses_class_default: {e}")
        return

    # wrong-sql class → full even in auto mode
    effective, label = _resolve_compact("auto", "ollama", "gemma4:12b-it-q8_0")
    assert effective is False, "wrong-sql class must force the full prompt"
    assert "model class" in label

    # works class → no forced mode, falls back to the usual auto heuristic
    eff_w, lbl_w = _resolve_compact("auto", "ollama", "qwen3.8:27b")
    assert "(model class)" not in lbl_w

    # forced radio values still beat the class default
    assert _resolve_compact("compact", "ollama", "gemma4:12b-it-q8_0") == (True, "compact (forced)")
    assert _resolve_compact("full", "ollama", "gpt-oss:20b") == (False, "full (forced)")

    # non-local providers are unaffected
    assert _resolve_compact("auto", "anthropic", "claude-sonnet-4-5") == (False, "full")


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
