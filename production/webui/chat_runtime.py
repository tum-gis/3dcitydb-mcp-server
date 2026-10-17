"""Gradio-independent chatbot conversation runtime shared by UI and CLI clients."""

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Generator

from webui.llm_utils import (
    CHAT_INSTRUCTIONS,
    _estimate_tokens,
    _get_provider_ctx_limit,
    _litellm_kwargs,
    safe_completion,
    should_use_compact,
)
from webui.model_profiles import profile_for_model

_LOCAL_PROVIDERS = ("ollama",)
_MAX_CONTEXT_CHARS = 400_000
_COUNT_PATTERN = re.compile(
    r"^\s*(there (are|is)|es gibt|il y a)\s+\d+\b.*\.\s*$",
    re.IGNORECASE,
)
_TOOL_CACHE_TTL_SEC = 300
_TOOL_CACHE_ROW_CAP = 30
_TOOL_CACHE_CHAR_CAP = 6000


@dataclass(frozen=True)
class ChatTurnSettings:
    """Provider and inference settings for one chatbot turn."""

    provider: str
    model: str
    temperature: float | None = None
    enable_thinking: bool | str = False
    prompt_mode: str = "auto"
    num_ctx: int | None = None


@dataclass(frozen=True)
class PreparedChatTurn:
    """Fully assembled and context-capped model input plus its measurements."""

    messages: list[dict]
    effective_compact: bool
    prompt_mode_label: str
    context_limit: int
    estimated_tokens: int
    dropped_messages: int


def build_tool_cache(sql: str, all_rows: list, row_count: int) -> dict | None:
    """Create a compact reusable result entry from a successful tool call."""
    if not sql or row_count <= 0 or not all_rows:
        return None
    return {
        "sql": sql,
        "rows": all_rows[:_TOOL_CACHE_ROW_CAP],
        "row_count": row_count,
        "truncated": len(all_rows) > _TOOL_CACHE_ROW_CAP,
        "ts": time.time(),
    }


def format_tool_cache_note(cache: dict) -> str:
    """Render a previous tool result as trusted system-side context."""
    age = max(0, int(time.time() - cache.get("ts", time.time())))
    rows_json = json.dumps(cache["rows"], default=str, ensure_ascii=False)
    if len(rows_json) > _TOOL_CACHE_CHAR_CAP:
        rows_json = rows_json[:_TOOL_CACHE_CHAR_CAP] + "…(truncated)"
    rows_json = rows_json.replace("{", "{{").replace("}", "}}")
    return (
        "PREVIOUS QUERY RESULT (from {age}s ago, still considered fresh)\n"
        "SQL:\n```sql\n{sql}\n```\n"
        "Returned {n} row(s){trunc}:\n"
        "{rows}\n\n"
        "GUIDANCE: If the user's follow-up references this exact result "
        "(\"which ones?\", \"show me\", \"explain\", \"format as a table\", etc.), "
        "answer directly from these rows — do NOT re-issue the same query. "
        "Re-query only if the follow-up needs different columns, a different "
        "filter, or fresh data."
    ).format(
        age=age,
        sql=cache["sql"].strip(),
        n=cache["row_count"],
        trunc=(
            f" (showing first {_TOOL_CACHE_ROW_CAP})"
            if cache.get("truncated")
            else ""
        ),
        rows=rows_json,
    )


def cache_is_fresh(cache: dict | None) -> bool:
    """Return whether a previous query result is still within the cache TTL."""
    return bool(
        cache
        and isinstance(cache, dict)
        and time.time() - cache.get("ts", 0) <= _TOOL_CACHE_TTL_SEC
    )


def resolve_compact(prompt_mode: str, provider: str, model: str) -> tuple[bool, str]:
    """Resolve the effective system-prompt size for a provider/model."""
    if provider not in _LOCAL_PROVIDERS:
        return False, "full"
    if prompt_mode == "compact":
        return True, "compact (forced)"
    if prompt_mode == "full":
        return False, "full (forced)"
    profile = profile_for_model(model)
    if profile.default_prompt_mode in ("compact", "full"):
        use_compact = profile.default_prompt_mode == "compact"
        return use_compact, f"{profile.default_prompt_mode} (model class)"
    use_compact = should_use_compact(provider, model)
    label = "compact (auto)" if use_compact else "full (auto)"
    return use_compact, label


def trim_messages(
    messages: list[dict], token_limit: int = 0
) -> tuple[list[dict], int]:
    """Drop oldest non-system messages to fit the same WebGUI context budget."""
    system = [message for message in messages if message["role"] == "system"]
    others = [message for message in messages if message["role"] != "system"]
    kept: list[dict] = []

    if token_limit > 0:
        effective = int(token_limit * 0.80)
        used = sum(len(str(message.get("content") or "")) for message in system) // 3
        for message in reversed(others):
            cost = len(str(message.get("content") or "")) // 3
            if used + cost > effective:
                break
            kept.insert(0, message)
            used += cost
    else:
        used = sum(len(str(message.get("content") or "")) for message in system)
        for message in reversed(others):
            cost = len(str(message.get("content") or ""))
            if used + cost > _MAX_CONTEXT_CHARS:
                break
            kept.insert(0, message)
            used += cost

    dropped = len(others) - len(kept)
    if dropped:
        print(
            f"[chat] context trim: dropped {dropped} oldest turn(s) to fit within limit",
            flush=True,
        )
    return system + kept, dropped


def build_messages(
    user_message: str,
    history: list,
    system_prompt: str,
    *,
    reasoning_history: list | None = None,
    story_history: list | None = None,
    reasoning_replay: bool = False,
    add_story: bool = False,
    leading_system_notes: list[str] | None = None,
) -> list[dict]:
    """Build provider-neutral messages for a new user turn.

    ``history`` uses the WebGUI's ``[user, assistant]`` turn representation.
    Optional notes are inserted immediately after the main system prompt, in
    their supplied order.
    """
    reasoning_history = reasoning_history or []
    story_history = story_history or []
    messages: list[dict] = [
        {"role": "system", "content": CHAT_INSTRUCTIONS + "\n\n" + system_prompt}
    ]

    for index, (user_msg, assistant_msg) in enumerate(history):
        if user_msg:
            messages.append({"role": "user", "content": user_msg})
        if (
            assistant_msg
            and not assistant_msg.startswith("*")
            and not assistant_msg.startswith("●")
        ):
            if _COUNT_PATTERN.match(assistant_msg.strip()) and len(assistant_msg) < 200:
                continue
            content = assistant_msg
            if (
                reasoning_replay
                and index < len(reasoning_history)
                and reasoning_history[index]
            ):
                content = (
                    f"[Reasoning]\n{reasoning_history[index]}\n\n[Answer]\n"
                    f"{assistant_msg}"
                )
            messages.append({"role": "assistant", "content": content})
            if add_story and index < len(story_history) and story_history[index]:
                messages.append({
                    "role": "system",
                    "content": (
                        "[AUTO-GENERATED SUMMARY — system context only. "
                        "This block was appended automatically after the "
                        "preceding assistant answer by a separate process; "
                        "the user never saw it. It exists solely to help "
                        "answer follow-up questions. Never quote, repeat, "
                        "imitate, or reformat it in your answers. Note that "
                        "row counts are condition-bound.]\n"
                        + story_history[index]
                    ),
                })

    messages.append({"role": "user", "content": user_message})
    for note in reversed(leading_system_notes or []):
        messages.insert(1, {"role": "system", "content": note})
    return messages


def prepare_chat_turn(
    user_message: str,
    history: list,
    system_prompt: str,
    settings: ChatTurnSettings,
    *,
    reasoning_history: list | None = None,
    story_history: list | None = None,
    reasoning_replay: bool = False,
    add_story: bool = False,
    leading_system_notes: list[str] | None = None,
) -> PreparedChatTurn:
    """Resolve prompt mode, construct messages, cap context, and measure usage."""
    effective_compact, mode_label = resolve_compact(
        settings.prompt_mode, settings.provider, settings.model
    )
    messages = build_messages(
        user_message,
        history,
        system_prompt,
        reasoning_history=reasoning_history,
        story_history=story_history,
        reasoning_replay=reasoning_replay,
        add_story=add_story,
        leading_system_notes=leading_system_notes,
    )
    context_limit = settings.num_ctx or _get_provider_ctx_limit(
        settings.provider, settings.model
    )
    messages, dropped = trim_messages(messages, token_limit=context_limit)
    return PreparedChatTurn(
        messages=messages,
        effective_compact=effective_compact,
        prompt_mode_label=mode_label,
        context_limit=context_limit,
        estimated_tokens=_estimate_tokens(messages),
        dropped_messages=dropped,
    )


def stream_chat_turn(
    settings: ChatTurnSettings,
    messages: list[dict],
    tool_executor: Callable[[str], str],
) -> Generator[tuple, None, None]:
    """Run a prepared turn through the production provider/backend dispatcher."""
    from webui.backends import stream

    enable_thinking = settings.enable_thinking
    if enable_thinking in ("off", ""):
        enable_thinking = False
    yield from stream(
        settings.provider,
        settings.model,
        settings.temperature,
        messages,
        tool_executor=tool_executor,
        enable_thinking=enable_thinking,
        num_ctx=settings.num_ctx,
    )


def build_reasoning_transcript(events: list) -> str:
    """Reconstruct reasoning and tool observations from backend events."""
    parts: list[str] = []
    buffer = ""
    for event, data in events:
        if event in ("thinking_token", "thinking"):
            if event == "thinking" and buffer.strip():
                parts.append(buffer.strip())
                buffer = ""
            buffer += data
        elif event == "tool_call":
            if buffer.strip():
                parts.append(buffer.strip())
                buffer = ""
            sql = data.get("args", {}).get("sql", "")
            parts.append(f"[Action] run_query: {sql}")
        elif event == "tool_result":
            if data.get("error"):
                parts.append(f"[Observation] Error: {data['error']}")
            else:
                parts.append(f"[Observation] {data.get('row_count', 0)} row(s) returned")
    if buffer.strip():
        parts.append(buffer.strip())
    return "\n".join(parts)


def _trim_transcript_for_story(transcript: str) -> str:
    max_chars = int(os.environ.get("STORY_TRANSCRIPT_MAX_CHARS", "30000"))
    if len(transcript) <= max_chars:
        return transcript
    return "[…] earlier trace truncated […]\n" + transcript[-max_chars:]


def _story_prompt(transcript: str, question: str) -> str:
    trace = _trim_transcript_for_story(transcript)
    return (
        "A database assistant answered the user question below. Its full "
        "reasoning trace (including abandoned, failed, or superseded "
        "approaches) is provided after it. If the trace starts with a "
        "truncation marker, the missing earlier part contained exploration "
        "or failed branches — base the story only on the visible steps.\n\n"
        f"USER QUESTION: {question[:500]}\n\n"
        f"REASONING TRACE:\n{trace}\n\n"
        "Write a compact story of ONLY the successful path that led to the "
        "final answer, using exactly this structure:\n"
        "1. <one short line: what this step achieved>\n"
        "   SQL: <the SQL of this step, VERBATIM — copy it exactly, never "
        "abbreviate, paraphrase, or truncate it>\n"
        "   Result: <one or two lines: row count, the key values used, any "
        "notable anomaly>\n"
        "2. … (repeat per successful step, in order)\n"
        "End with one 'Avoid:' line per abandoned or failed approach worth "
        "remembering (one line each, e.g. a wrong column or code mapping the "
        "agent corrected). If none, write 'Avoid: none'.\n"
        "Rules:\n"
        "- Do NOT include SQL from abandoned branches as numbered steps — "
        "only as 'Avoid:' warnings.\n"
        "- The SQL lines must be byte-for-byte identical to the trace.\n"
        "- Make explicit WHY the final query works: the key table/column "
        "mappings it relies on.\n"
        "- Prose budget: at most 300 words outside the SQL blocks. No extra "
        "sections, no restatement of the final answer."
    )


def summarize_story_ollama(
    transcript: str, question: str, model: str, num_ctx: int | None
) -> tuple[str, str]:
    """Distill the successful reasoning path with the selected Ollama model."""
    if not transcript.strip():
        return "", "no reasoning transcript to distill"

    from langchain_ollama import ChatOllama

    prompt = _story_prompt(transcript, question)
    try:
        llm = ChatOllama(
            model=model,
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            temperature=0.0,
            timeout=float(os.environ.get("OLLAMA_TIMEOUT", "300")),
            num_predict=700,
            reasoning=False,
            model_kwargs={"num_ctx": num_ctx or 131072},
        )
        result = llm.invoke([{"role": "user", "content": prompt}])
        text = (result.content or "").strip()
        if not text:
            return "", "model returned an empty story"
        return text, ""
    except Exception as exc:
        print(f"[chat] story summarization failed: {exc}", flush=True)
        return "", f"{type(exc).__name__}: {exc}"


def summarize_story_openai(
    transcript: str, question: str, model: str
) -> tuple[str, str]:
    """Distill the successful reasoning path through the OpenAI backend."""
    if not transcript.strip():
        return "", "no reasoning transcript to distill"

    prompt = _story_prompt(transcript, question)
    try:
        kw = _litellm_kwargs("openai", model, None, enable_thinking=False)
        kw["max_tokens"] = int(os.environ.get("STORY_MAX_TOKENS", "2000"))
        kw["extra_body"] = {"reasoning_effort": "none"}
        for attempt in (1, 2):
            resp = safe_completion(kw, messages=[{"role": "user", "content": prompt}])
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return text, ""
            finish = resp.choices[0].finish_reason
            print(
                f"[chat] story summarization (openai) attempt {attempt} "
                f"returned empty content (finish_reason={finish})",
                flush=True,
            )
        return "", "model returned an empty story"
    except Exception as exc:
        print(f"[chat] story summarization (openai) failed: {exc}", flush=True)
        return "", f"{type(exc).__name__}: {exc}"


def summarize_story(
    provider: str,
    transcript: str,
    question: str,
    model: str,
    num_ctx: int | None = None,
) -> tuple[str, str]:
    """Dispatch optional turn-story distillation using existing provider rules."""
    if provider == "openai":
        return summarize_story_openai(transcript, question, model)
    if provider == "ollama":
        return summarize_story_ollama(transcript, question, model, num_ctx)
    return "", f"story distillation is not supported for provider {provider!r}"
