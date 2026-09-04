"""Empirical model-class registry for local (Ollama) models (plan step e).

Derived from the local-model probe matrix — see
``production/docs/local-model-probing.md`` for the full results table and
root-cause analysis.

Five empirical failure/quality classes (plus ``unknown``), mapped from
model-name prefixes (longest matching prefix wins, case-insensitive):

- ``works``               → recommended, no warning.
- ``sentence-as-tool``    → recommended *after* the step-a parser fix: the
                            model names the Action with a sentence, which the
                            robust parser now auto-repairs to ``run_query``.
                            Informational note only.
- ``wrong-sql``           → model emits syntactically valid but semantically
                            wrong SQL (e.g. missing CG_MakeSolid → all-zero
                            volumes; or picking a child-property value column
                            instead of CG_Volume). Default prompt mode ``full``
                            (stronger schema emphasis). Recommended with a
                            verify-the-metric warning.
- ``thinking-then-empty`` → upstream Ollama/gemma4 bug: a full query plan is
                            produced in the reasoning stream, then the answer
                            stays empty. Not recommended.
- ``reasoning-leak``      → model may stream its reasoning as the answer.
                            Not recommended. (Reserved — no currently probed
                            model is in this class; gpt-oss:20b was moved to
                            ``wrong-sql`` after the step-b parser fix made its
                            reasoning dumps recoverable.)
- ``unknown``             → no warning, no special handling.
"""

from dataclasses import dataclass

# ── Per-class display data ────────────────────────────────────────────────────
# warning: text shown in the warning slot next to the model dropdown ("" = none).
# recommended: informational flag (also surfaced in the warning text).
# default_prompt_mode: "auto" | "compact" | "full" — used by app.py when the
# user leaves the prompt-mode radio on "auto".

_WORKS_NOTE = ""
_SENTENCE_NOTE = (
    "ℹ️ This model sometimes names the tool call with a sentence instead of "
    "`run_query`. The parser auto-repairs this — **recommended**."
)
_WRONG_SQL_NOTE = (
    "⚠️ This model tends to emit semantically wrong SQL (all-zero results). "
    "Prompt mode is forced to **full** for stronger schema emphasis."
)
_WRONG_SQL_METRIC_NOTE = (
    "⚠️ This model emits valid SQL but sometimes picks the wrong metric "
    "(e.g. a child-property value column instead of ``CG_Volume``) and stops "
    "after one iteration — **verify the numbers before trusting the answer**. "
    "Prompt mode is forced to **full** for stronger schema emphasis."
)
_THINKING_EMPTY_NOTE = (
    "⚠️ Not recommended: this model completes reasoning but delivers an "
    "**empty answer** on this Ollama version (known gemma4 bug, upstream "
    "Ollama 0.33.x)."
)
_LEAK_NOTE = (
    "⚠️ Not recommended: this model **may leak reasoning as the answer** "
    "(its reasoning stream is emitted as ordinary text)."
)


@dataclass(frozen=True)
class ModelProfile:
    """Classification result for a model name."""

    model_class: str            # one of the six class names above
    warning: str                # markdown for the warning slot ("" = none)
    recommended: bool
    default_prompt_mode: str    # "auto" | "compact" | "full"


_UNKNOWN = ModelProfile(
    model_class="unknown", warning="", recommended=True, default_prompt_mode="auto",
)


# Prefix → (model_class, warning, recommended, default_prompt_mode).
# Longest matching prefix wins. Keep entries conservative: only models that
# were actually probed belong here.
_PREFIX_PROFILES: dict[str, ModelProfile] = {
    # ── works ──────────────────────────────────────────────────────────────
    "qwen3.8": ModelProfile("works", _WORKS_NOTE, True, "auto"),
    "qwen3.6": ModelProfile("works", _WORKS_NOTE, True, "auto"),
    "ministral-3": ModelProfile("works", _WORKS_NOTE, True, "auto"),
    "nemotron-3-nano": ModelProfile("works", _WORKS_NOTE, True, "auto"),
    "phi4": ModelProfile("works", _WORKS_NOTE, True, "auto"),
    # ── sentence-as-tool (auto-repaired by the step-a parser fix) ─────────
    "granite4.2": ModelProfile(
        "sentence-as-tool", _SENTENCE_NOTE, True, "auto",
    ),
    # ── wrong-sql ─────────────────────────────────────────────────────────
    "gemma4:12b": ModelProfile(
        "wrong-sql", _WRONG_SQL_NOTE, True, "full",
    ),
    # gpt-oss:20b: the parser fix recovers its tool_calls / inline SQL from
    # reasoning dumps, so the tool now runs reliably; the remaining risk is
    # picking the wrong SQL metric (child.val_double vs CG_Volume) and
    # stopping after one successful-but-wrong iteration. See
    # production/docs/local-model-probing.md.
    "gpt-oss": ModelProfile(
        "wrong-sql", _WRONG_SQL_METRIC_NOTE, True, "full",
    ),
    # ── thinking-then-empty (upstream Ollama/gemma4 bug) ──────────────────
    "gemma4:26b": ModelProfile(
        "thinking-then-empty", _THINKING_EMPTY_NOTE, False, "auto",
    ),
    "gemma4:31b": ModelProfile(
        "thinking-then-empty", _THINKING_EMPTY_NOTE, False, "auto",
    ),
}


def profile_for_model(model: str) -> ModelProfile:
    """Classify a model name (longest matching prefix wins; unknown → unknown)."""
    m = (model or "").strip().lower()
    if not m:
        return _UNKNOWN
    best = _UNKNOWN
    best_len = -1
    for prefix, prof in _PREFIX_PROFILES.items():
        if m.startswith(prefix) and len(prefix) > best_len:
            best = prof
            best_len = len(prefix)
    return best
