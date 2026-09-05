"""Unit tests for the per-model LLM kwargs registry (plan step c).

Run from the repo root (host):
    python tests/test_local_kwargs.py
or inside the agent image (has langchain):
    docker run --rm -v <repo>:/repo citydb-mcp-agent:local python /repo/tests/test_local_kwargs.py

Contract under test:
  * lookup returns the right overrides for sample model strings
  * unknown models pass through unchanged
  * explicit user values (anything other than the UI default) always win
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

from webui.backends.local import (  # noqa: E402
    _MODEL_KWARG_DEFAULTS,
    _resolve_llm_kwargs,
)

# UI defaults a user who never touched the controls produces.
UI_T = 0.1
UI_THINKING = False
UI_CTX = 65536


def test_registry_shape() -> None:
    """Every registry entry only mentions allowed keys, with sane values."""
    allowed = {"temperature", "thinking", "num_ctx"}
    for prefix, spec in _MODEL_KWARG_DEFAULTS.items():
        assert prefix, "prefix key must not be empty"
        assert set(spec) <= allowed, f"{prefix}: unknown keys {set(spec) - allowed}"
        if "temperature" in spec:
            assert 0.0 <= spec["temperature"] <= 1.0
        if "thinking" in spec:
            assert spec["thinking"] in (None, False, "low", "medium", "high")
        if "num_ctx" in spec:
            assert spec["num_ctx"] >= 4096


def test_gpt_oss_defaults_applied_at_ui_defaults() -> None:
    """gpt-oss:20b at untouched UI settings gets its profile applied.

    think must be omitted entirely (None): think:false measurably degrades
    tool-calling reliability on the real langchain path (3/6 vs 4/4).
    """
    t, th, ctx = _resolve_llm_kwargs("gpt-oss:20b", UI_T, UI_THINKING, UI_CTX)
    assert (t, th, ctx) == (_MODEL_KWARG_DEFAULTS["gpt-oss"]["temperature"],
                            _MODEL_KWARG_DEFAULTS["gpt-oss"]["thinking"], UI_CTX)
    assert th is None


def test_gemma4_thinking_off_for_26b_and_31b() -> None:
    """Both probed gemma4 thinking-then-empty models: thinking stays off."""
    for m in ("gemma4:26b-a4b-it-q4_K_M", "gemma4:31b-it-q4_K_M",
              "gemma4:26b", "gemma4:31b"):
        t, th, ctx = _resolve_llm_kwargs(m, UI_T, UI_THINKING, UI_CTX)
        assert th is False, f"{m}: expected thinking=False"


def test_unknown_model_passthrough() -> None:
    """No matching prefix → every value passes through unchanged."""
    for m in ("qwen3.8:27b", "qwen3.6:35b-a3b-q4_K_M", "ministral-3:14b",
              "nemotron-3-nano:latest", "phi4:14b-q4_K_M",
              "granite4.2:30b-q4_K_M", "gemma4:12b-it-q8_0",
              "totally/unknown-model:0"):
        t, th, ctx = _resolve_llm_kwargs(m, 0.7, "high", 32768)
        assert (t, th, ctx) == (0.7, "high", 32768), f"{m} must pass through"
    # think=omitted (None) must also pass through untouched.
    t, th, ctx = _resolve_llm_kwargs("totally/unknown-model:0", 0.7, None, 32768)
    assert th is None, "None thinking must pass through for unknown models"


def test_unset_temperature_stays_unset() -> None:
    """An unchecked temperature control must not activate model defaults."""
    t, th, ctx = _resolve_llm_kwargs("gpt-oss:20b", None, UI_THINKING, UI_CTX)
    assert t is None
    assert th is None
    assert ctx == UI_CTX


def test_user_temperature_override_wins() -> None:
    """Any temperature other than the UI default beats the profile value."""
    for m in ("gpt-oss:20b", "gemma4:26b-a4b-it-q4_K_M"):
        t, _, _ = _resolve_llm_kwargs(m, 0.4, UI_THINKING, UI_CTX)
        assert t == 0.4, f"{m}: user temperature must win"
        t, _, _ = _resolve_llm_kwargs(m, 0.0, UI_THINKING, UI_CTX)
        assert t == 0.0, f"{m}: user temperature must win"


def test_user_thinking_override_wins() -> None:
    """A user-selected thinking level beats a profile thinking=False."""
    for m in ("gpt-oss:20b", "gemma4:31b-it-q4_K_M"):
        for level in ("low", "medium", "high"):
            _, th, _ = _resolve_llm_kwargs(m, UI_T, level, UI_CTX)
            assert th == level, f"{m}: user thinking level must win"


def test_user_num_ctx_override_wins() -> None:
    """A non-default context selection beats a profile num_ctx (if any)."""
    _, _, ctx = _resolve_llm_kwargs("gpt-oss:20b", UI_T, UI_THINKING, 32768)
    assert ctx == 32768
    # num_ctx=None (unset) passes through untouched as well.
    _, _, ctx = _resolve_llm_kwargs("gpt-oss:20b", UI_T, UI_THINKING, None)
    assert ctx is None


def test_longest_prefix_wins() -> None:
    """If two prefixes match, the longer (more specific) one must win."""
    # 'gemma4:26b' is a prefix of 'gemma4:26b-a4b-it-q4_K_M'; make sure the
    # lookup is not confused by the extra ':' variants in the dict.
    t, th, _ = _resolve_llm_kwargs("gemma4:26b-a4b-it-q4_K_M", UI_T, UI_THINKING, UI_CTX)
    assert th is False
    # gemma4:12b has NO profile — it must not inherit one from a sibling tag.
    t, th, _ = _resolve_llm_kwargs("gemma4:12b-it-q8_0", UI_T, UI_THINKING, UI_CTX)
    assert th is UI_THINKING  # unchanged (False == False, but via passthrough)
    assert t == UI_T


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
