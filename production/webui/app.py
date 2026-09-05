"""Gradio chat UI for citydb-mcp."""

import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Generator

# ── gradio_client boolean-schema fix ──────────────────────────────────────────
try:
    import gradio_client.utils as _gcu

    _orig_json_schema = _gcu._json_schema_to_python_type
    def _patched_json_schema(schema, defs=None):
        if isinstance(schema, bool):
            return "bool"
        return _orig_json_schema(schema, defs)
    _gcu._json_schema_to_python_type = _patched_json_schema

    _orig_get_type = _gcu.get_type
    def _patched_get_type(schema):
        if isinstance(schema, bool):
            return "bool"
        return _orig_get_type(schema)
    _gcu.get_type = _patched_get_type
except Exception as _e:
    print(f"Warning: could not patch gradio_client: {_e}")
# ──────────────────────────────────────────────────────────────────────────────

from dotenv import load_dotenv
load_dotenv(override=False)

import gradio as gr

from webui.llm_utils import (
    ANTHROPIC_MODELS, OPENAI_MODELS, CHAT_INSTRUCTIONS,
    detect_default_provider, models_for_provider,
    get_ollama_models, get_openai_ollama_models,
    _rows_to_markdown_table,
    extract_highlight_payload, fallback_regex_extract,
    _compute_centroid_wgs84,
    request_stop, _clear_stop, _is_stopped,
    _get_provider_ctx_limit, _estimate_tokens, _log_context_usage,
    _ollama_reachable,
    should_use_compact,
)
from webui.backends import stream as agent_stream
from webui.model_profiles import profile_for_model
from webui.mcp_client import assemble_system_prompt_sync, run_tool_sync

_LOCAL_PROVIDERS = ("ollama",)


def _history_with_duration(history: list, started: float) -> list:
  """Return a display-only history copy with the elapsed time appended."""
  display_history = [row[:] for row in history]
  if display_history and len(display_history[-1]) > 1:
    elapsed = time.perf_counter() - started
    display_history[-1][1] = (
      f"{display_history[-1][1]}\n\n"
      f"*Time to answer the question {elapsed:.3f} seconds*"
    )
  return display_history

_CTX_OPTIONS = ["8K (8,192)", "32K (32,768)",  "64K (65,536)","128K (131,072)", "256K (262,144)"]
_CTX_VALUES = {"8K (8,192)": 8192, "32K (32,768)": 32768, "64K (65,536)": 65536, "128K (131,072)": 131072, "256K (262,144)": 262144}
_CTX_DEFAULT = "64K (65,536)"

VARIANT = os.environ.get("CITYDB_MCP_VARIANT", "byod")
ENABLE_VIZ = os.environ.get("ENABLE_VIZ", "false").lower() == "true"

_MAX_CONTEXT_CHARS = 400_000

# ── Last-tool-result cache (per Gradio session) ───────────────────────────────
# When the user asks a follow-up like "which ones?" we want the model to be
# able to reference the previous tool result without re-querying. The cache
# is kept in a gr.State; it is updated on every successful tool result and
# injected as a system-side hint into the next turn's messages.
_TOOL_CACHE_TTL_SEC = 300       # 5 minutes; older entries are dropped.
_TOOL_CACHE_ROW_CAP = 30        # max rows carried into next turn.
_TOOL_CACHE_CHAR_CAP = 6000     # hard ceiling on the injected block.


def _build_tool_cache(sql: str, all_rows: list, row_count: int) -> dict | None:
    """Make a compact cache entry. Returns None if there's nothing to cache."""
    import time as _t
    if not sql or row_count <= 0 or not all_rows:
        return None
    rows = all_rows[:_TOOL_CACHE_ROW_CAP]
    truncated = len(all_rows) > _TOOL_CACHE_ROW_CAP
    return {
        "sql": sql,
        "rows": rows,
        "row_count": row_count,
        "truncated": truncated,
        "ts": _t.time(),
    }


def _format_tool_cache_note(cache: dict) -> str:
    """Render the cache as a system-side hint for the next turn."""
    import json as _json
    import time as _t
    age = max(0, int(_t.time() - cache.get("ts", _t.time())))
    rows_json = _json.dumps(cache["rows"], default=str, ensure_ascii=False)
    if len(rows_json) > _TOOL_CACHE_CHAR_CAP:
        rows_json = rows_json[: _TOOL_CACHE_CHAR_CAP] + "…(truncated)"
    # LangChain ChatPromptTemplate interprets {name} as a template variable.
    # Double-escape all curly braces in the JSON so they pass through as literals.
    rows_json = rows_json.replace("{", "{{").replace("}", "}}")
    note = (
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
        trunc=(f" (showing first {_TOOL_CACHE_ROW_CAP})" if cache.get("truncated") else ""),
        rows=rows_json,
    )
    return note


def _cache_is_fresh(cache: dict | None) -> bool:
    if not cache or not isinstance(cache, dict):
        return False
    import time as _t
    return (_t.time() - cache.get("ts", 0)) <= _TOOL_CACHE_TTL_SEC


def _trim_messages(
    messages: list[dict], token_limit: int = 0
) -> tuple[list[dict], int]:
    """Drop oldest history turns (never the system prompt) to fit within the limit."""
    system = [m for m in messages if m["role"] == "system"]
    others = [m for m in messages if m["role"] != "system"]
    kept: list[dict] = []

    if token_limit > 0:
        effective = int(token_limit * 0.80)
        used = sum(len(str(m.get("content") or "")) for m in system) // 3
        for msg in reversed(others):
            cost = len(str(msg.get("content") or "")) // 3
            if used + cost > effective:
                break
            kept.insert(0, msg)
            used += cost
    else:
        used = sum(len(str(m.get("content") or "")) for m in system)
        for msg in reversed(others):
            cost = len(str(msg.get("content") or ""))
            if used + cost > _MAX_CONTEXT_CHARS:
                break
            kept.insert(0, msg)
            used += cost

    dropped = len(others) - len(kept)
    if dropped:
        print(
            f"[chat] context trim: dropped {dropped} oldest turn(s) to fit within limit",
            flush=True,
        )
    return system + kept, dropped


# ── System prompt cache (supports compact mode) ────────────────────────────────
_system_prompt_cache: dict = {}
_sp_lock = threading.Lock()


def _get_system_prompt(compact: bool = False) -> str:
    cache_key = "compact" if compact else "full"
    with _sp_lock:
        if cache_key not in _system_prompt_cache:
            try:
                _system_prompt_cache[cache_key] = assemble_system_prompt_sync(
                    include_query_agent_extras=True,
                    compact=compact,
                )
                size = len(_system_prompt_cache[cache_key])
                print(f"[prompt] compact={compact}, size={size} chars", flush=True)
            except Exception as exc:
                _system_prompt_cache[cache_key] = (
                    f"[Warning: could not assemble system prompt: {exc}]"
                )
        return _system_prompt_cache[cache_key]


def _refresh_system_prompt() -> None:
    with _sp_lock:
        _system_prompt_cache.clear()
    _get_system_prompt()


# ── Status checks ──────────────────────────────────────────────────────────────

def _check_db_status() -> str:
    """Return 'ok', 'empty', or 'unreachable'."""
    import json as _json
    try:
        raw = run_tool_sync("run_query", {"sql": "SELECT COUNT(*) AS n FROM feature"})
        try:
            data = _json.loads(raw)
            if data.get("error"):
                return "unreachable"
            rows = data.get("preview_rows") or data.get("all_rows") or []
            if rows and int(rows[0].get("n", 1)) == 0:
                return "empty"
        except Exception:
            pass
        return "ok"
    except Exception:
        return "unreachable"


def _check_mcp_status() -> bool:
    try:
        run_tool_sync("get_lod_config", {})
        return True
    except Exception:
        return False


def _check_provider_status(provider: str, model: str) -> bool:
    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if provider == "openai":
        return bool(
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("OPENAI_API_BASE")
        )
    if provider == "ollama":
        return _ollama_reachable()
    return False


def _dot(ok: bool) -> str:
    return "🟢" if ok else "🔴"


def _dot_db(status: str) -> str:
    if status == "ok":
        return "🟢"
    if status == "empty":
        return "🟠"
    return "🔴"


_DB_EMPTY_WARNING = (
    '<div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;'
    'padding:10px 14px;margin-bottom:8px;color:#9a3412;font-size:0.87rem;">'
    "⚠️ <strong>The database is empty</strong> — please import CityGML data first, "
    "or click <strong>Refresh assembled prompt</strong> in the MCP Inspector tab "
    "if you are sure the CityGML was already imported."
    "</div>"
)


def _make_ctx_bar(n_tok: int, ctx_limit: int) -> str:
    """Build the context-window progress-bar HTML for the sidebar."""
    pct = min((n_tok / ctx_limit * 100) if ctx_limit > 0 else 0, 100)
    if pct <= 50:
        r = int(pct / 50 * 255)
        g = 210
    elif pct <= 80:
        r = 255
        g = int(210 - (pct - 50) / 30 * 160)
    else:
        r = 220
        g = int(50 - (pct - 80) / 20 * 50)
    g = max(0, g)
    bar_color = f"rgb({r},{g},40)"
    warn = " ⚠️" if pct > 80 else ""
    return (
        f'<div style="font-size:0.75rem;color:#94a3b8;padding:2px 0 2px;">Context window{warn}</div>'
        f'<div style="background:#1e293b;border-radius:4px;height:8px;overflow:hidden;margin-bottom:3px;">'
        f'  <div style="width:{pct:.1f}%;height:100%;background:{bar_color};'
        f'border-radius:4px;transition:width 0.4s ease,background 0.4s ease;"></div>'
        f'</div>'
        f'<div style="font-size:0.72rem;color:#64748b;padding-bottom:6px;">'
        f'{n_tok:,}&thinsp;/&thinsp;{ctx_limit:,} tokens ({pct:.0f}%)</div>'
    )


def _log_nav_html(page: int, total: int) -> str:
    if total == 0:
        return '<div style="text-align:center;font-size:0.78rem;color:#94a3b8;padding:2px 0;">—</div>'
    return (
        f'<div style="text-align:center;font-size:0.78rem;color:#94a3b8;padding:2px 0;">'
        f'Query {total - page + 1} / {total}</div>'
    )


def get_status_html(provider: str = "", model: str = "", prompt_mode_label: str = "", db_status: str | None = None) -> str:
    if db_status is None:
        db_status = _check_db_status()
    mcp_ok = _check_mcp_status()
    prov_ok = _check_provider_status(provider, model) if provider else False
    prov_label = f"Provider ({provider})" if provider else "Provider"
    mode_span = f'<span>📄 {prompt_mode_label}</span>' if prompt_mode_label else ""
    return (
        f'<div style="display:flex;gap:16px;font-size:0.85rem;padding:6px 0;">'
        f'<span>{_dot_db(db_status)} DB</span>'
        f'<span>{_dot(mcp_ok)} MCP server</span>'
        f'<span>{_dot(prov_ok)} {prov_label}</span>'
        f'{mode_span}'
        f'</div>'
    )


# ── Compact-mode resolution ────────────────────────────────────────────────────

def _resolve_compact(prompt_mode: str, provider: str, model: str) -> tuple[bool, str]:
    """Return (effective_compact, label) for the given radio value + provider/model.

    label is one of: 'compact (auto)', 'compact (forced)', 'full (auto)', 'full (forced)'.
    """
    if provider not in _LOCAL_PROVIDERS:
        return False, "full"
    if prompt_mode == "compact":
        return True, "compact (forced)"
    if prompt_mode == "full":
        return False, "full (forced)"
    # auto — the model's empirical class may demand a specific mode
    # (e.g. wrong-sql models get the full prompt for stronger schema emphasis).
    profile = profile_for_model(model) if provider in _LOCAL_PROVIDERS else None
    if profile is not None and profile.default_prompt_mode in ("compact", "full"):
        use_compact = profile.default_prompt_mode == "compact"
        label = f"{profile.default_prompt_mode} (model class)"
        return use_compact, label
    use_compact = should_use_compact(provider, model)
    label = "compact (auto)" if use_compact else "full (auto)"
    return use_compact, label


# ── SQL extraction ─────────────────────────────────────────────────────────────
_SQL_RE = re.compile(r"```sql\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_sql(text: str) -> str:
    matches = _SQL_RE.findall(text)
    return matches[-1].strip() if matches else ""


# ── Chat logic ─────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _esc_angle(text: str) -> str:
    """Escape < and > so raw HTML from the model's reasoning (e.g. mermaid
    <<abstract>> stereotypes, --|> arrows) is rendered as literal text by
    gr.Markdown instead of being parsed as (malformed) HTML tags."""
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _build_reasoning_transcript(events: list) -> str:
    """Reconstruct a compact thought/action/observation transcript from the
    raw event stream (Ollama only — relies on local.py's thinking_token
    events). Used to optionally replay reasoning into future turns' context.
    """
    parts: list[str] = []
    buf = ""
    for event, data in events:
        if event == "thinking_token":
            buf += data
        elif event == "tool_call":
            if buf.strip():
                parts.append(buf.strip())
                buf = ""
            sql = data.get("args", {}).get("sql", "")
            parts.append(f"[Action] run_query: {sql}")
        elif event == "tool_result":
            if data.get("error"):
                parts.append(f"[Observation] Error: {data['error']}")
            else:
                parts.append(f"[Observation] {data.get('row_count', 0)} row(s) returned")
    if buf.strip():
        parts.append(buf.strip())
    return "\n".join(parts)


def _summarize_reasoning_ollama(transcript: str, model: str, num_ctx: int | None) -> tuple[str, str]:
    """Ask the same Ollama model to distill its own reasoning trace from this
    turn into a short "lessons learned" note, carried into future turns.

    Runs synchronously — adds one extra (blocking) generation after the turn
    that just finished. Returns (summary, error) — exactly one is truthy.
    On failure, or an empty/uninformative response, summary is "" and error
    explains why, so the caller can surface it instead of failing silently.
    """
    if not transcript.strip():
        return "", "no reasoning transcript to summarize"

    # langchain-ollama >= 0.3 maps `reasoning=False` to Ollama's top-level
    # "think": false, so the previous raw-urllib workaround (added because
    # 0.2.0 had no think/reasoning field at all) is no longer needed.
    from langchain_ollama import ChatOllama

    prompt = (
        "Below is your own reasoning trace from answering a database question, "
        "including any failed attempts and corrections:\n\n"
        f"{transcript[:8000]}\n\n"
        "In at most 3 short bullet points, note anything durable you learned that "
        "would help you answer similar questions faster next time (correct table/column "
        "mappings, working SQL patterns, mistakes to avoid). Be concise and factual — "
        "do not restate the question or the final answer. "
        "If there is nothing generalizable, reply with exactly: (nothing to note)"
    )
    try:
        llm = ChatOllama(
            model=model,
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            temperature=0.0,
            timeout=float(os.environ.get("OLLAMA_TIMEOUT", "300")),
            num_predict=500,
            reasoning=False,
            model_kwargs={"num_ctx": num_ctx or 32768},
        )
        result = llm.invoke([{"role": "user", "content": prompt}])
        text = (result.content or "").strip()
        if not text:
            return "", "model returned an empty response"
        if text.lower().startswith("(nothing"):
            return "", "model reported nothing generalizable to note"
        return text, ""
    except Exception as exc:
        print(f"[chat] lessons-learned summarization failed: {exc}", flush=True)
        return "", f"{type(exc).__name__}: {exc}"


def chat_stream(
    user_message: str,
    history: list,
    provider: str,
    model: str,
  set_temperature: bool,
    temperature: float,
    enable_thinking: bool | str,
    prompt_mode: str,
    num_ctx_label: str,
    log_history: list | None = None,
    tool_cache: dict | None = None,
    reasoning_replay: bool = False,
    add_lessons: bool = False,
    reasoning_history: list | None = None,
    lessons_note: str | None = None,
) -> Generator[tuple, None, None]:
    if log_history is None:
        log_history = []
    # Reasoning trace per turn (Ollama only), index-aligned with `history`.
    # See _build_reasoning_transcript / the two toggles below.
    reasoning_history = list(reasoning_history) if reasoning_history else []
    # Cache value to emit alongside every yield. Starts as whatever the UI
    # passed in; reassigned after a successful tool result this turn.
    cache_out = tool_cache if _cache_is_fresh(tool_cache) else None
    _pending_sql = ""
    print(f"[chat] history has {len(history)} turns", flush=True)

    if not set_temperature:
      temperature = None

    effective_compact, mode_label = _resolve_compact(prompt_mode, provider, model)
    print(f"[chat] prompt_mode={prompt_mode!r}  effective_compact={effective_compact}  ({mode_label})", flush=True)

    # Resolve num_ctx (only meaningful for local providers)
    num_ctx = _CTX_VALUES.get(num_ctx_label, 32768) if provider in _LOCAL_PROVIDERS else None

    # UI sends "off"|"low"|"medium"|"high" from the thinking dropdown;
    # backends expect False (disabled) or the level string (enabled).
    # "max" was never forwarded by the ollama client that langchain-ollama
    # pins (its think field is Literal['low','medium','high']) — map it to
    # "high" rather than surfacing a pydantic ValidationError.
    if enable_thinking in ("off", ""):
        enable_thinking = False
    elif enable_thinking == "max":
        enable_thinking = "high"

    _clear_stop()

    _NO_HL = gr.update()
    _NO_CTX = gr.update()

    if not user_message.strip():
        yield history, history, "*Idle.*", "", gr.update(), _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note
        return

    answer_started = time.perf_counter()
    stop_update = gr.update(visible=True)

    history = history + [[user_message, "● ● ●"]]
    # Enable "prev" immediately if earlier turns already exist, instead of
    # leaving the log-nav buttons frozen in their last-completed-turn state
    # for the whole duration of this (possibly slow, local-model) turn.
    yield history, history, "*Idle.*", "", stop_update, _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(interactive=len(log_history) > 0), gr.update(interactive=False), cache_out, reasoning_history, lessons_note

    history[-1][1] = "*Connecting to knowledge base…*"
    yield history, history, "*Idle.*", "", gr.update(), _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note
    sp = _get_system_prompt(compact=effective_compact)
    system_prompt = CHAT_INSTRUCTIONS + "\n\n" + sp
    total_chars = len(system_prompt)
    print(f"[prompt] sys: {len(system_prompt)} chars, msgs: {len(history)}, total: {total_chars}", flush=True)

    _COUNT_PATTERN = re.compile(
        r"^\s*(there (are|is)|es gibt|il y a)\s+\d+\b.*\.\s*$",
        re.IGNORECASE,
    )
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    _replay_reasoning = reasoning_replay and provider == "ollama"
    for idx, (user_msg, asst_msg) in enumerate(history[:-1]):
        if user_msg:
            messages.append({"role": "user", "content": user_msg})
        if asst_msg and not asst_msg.startswith("*") and not asst_msg.startswith("●"):
            if _COUNT_PATTERN.match(asst_msg.strip()) and len(asst_msg) < 200:
                continue
            content = asst_msg
            if _replay_reasoning and idx < len(reasoning_history) and reasoning_history[idx]:
                content = f"[Reasoning]\n{reasoning_history[idx]}\n\n[Answer]\n{asst_msg}"
            messages.append({"role": "assistant", "content": content})
    messages.append({"role": "user", "content": user_message})

    # Self-summarized "lessons learned" from previous turns (Ollama only).
    # Inserted before the tool-result cache note below so both survive
    # _trim_messages (which keeps all system-role messages).
    if add_lessons and provider == "ollama" and lessons_note:
        messages.insert(1, {"role": "system", "content": f"[Lessons learned from previous turns]\n{lessons_note}"})

    # If we have a fresh cache from the previous turn, inject it as an extra
    # system message right after the main system prompt. _trim_messages keeps
    # all system messages, so it survives history pruning.
    if cache_out is not None:
        messages.insert(1, {"role": "system", "content": _format_tool_cache_note(cache_out)})
        print(
            f"[chat] injected cached tool result: {cache_out['row_count']} rows, "
            f"age={int((__import__('time').time() - cache_out.get('ts', 0)))}s",
            flush=True,
        )

    ctx_limit = num_ctx if num_ctx else _get_provider_ctx_limit(provider, model)
    messages, dropped_turns = _trim_messages(messages, token_limit=ctx_limit)

    used_tok = _estimate_tokens(messages)
    ctx_bar_html = _make_ctx_bar(used_tok, ctx_limit)

    accumulated = ""
    trace_lines: list[str] = []
    got_content = False
    collected_events: list = []
    _first_status_seen = False

    def log(line: str) -> str:
        trace_lines.append(f"`{_ts()}` {line}")
        return "\n\n".join(trace_lines)

    # Live raw-token streaming into Agent Activity (Ollama only — see local.py's
    # thinking_token events). Appends into the current block until a tool call
    # or a new turn starts a fresh one.
    _thinking_stream_idx: int | None = None

    def _stream_thinking(token: str) -> str:
        nonlocal _thinking_stream_idx
        if _thinking_stream_idx is None:
            trace_lines.append(f"`{_ts()}` 🧠 **Reasoning (live):**\n\n")
            _thinking_stream_idx = len(trace_lines) - 1
        # Escape < / > so raw mermaid (<<abstract>>, --|>) in the streamed
        # reasoning is shown as literal text, not parsed as bogus HTML.
        trace_lines[_thinking_stream_idx] += _esc_angle(token).replace("\n", "  \n")
        return "\n\n".join(trace_lines)

    trace_md = log(f"**User query:** {user_message[:200]}")
    if dropped_turns:
        trace_md = log(f"⚠️ **Context trimmed:** dropped {dropped_turns} oldest turn(s) to fit within the {ctx_limit:,}-token context window.")
        ctx_warning = (
            f"⚠️ **Context window almost full.** The **{dropped_turns}** oldest "
            f"turn(s) have been removed from the model's memory to fit within the "
            f"{ctx_limit:,}-token limit. The system prompt and all recent turns are preserved. "
            f"Start a **New conversation** if you want a clean slate."
        )
        history = history + [[None, ctx_warning]]
    yield history, history, trace_md, "", gr.update(), _NO_HL, ctx_bar_html, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note

    def _render_tool_call(sql: str, it: int) -> str:
        return log(
            f"🔧 **Calling** `run_query` — iteration {it + 1}\n\n"
            f"```sql\n{sql}\n```"
        )

    def _render_tool_result(data: dict) -> str:
        rows = data.get("preview_rows", [])
        row_count = data.get("row_count", 0)
        err = data.get("error")
        it = data.get("iteration", 0)
        ms = data.get("execution_time_ms", 0)
        if err:
            return log(f"⚠️ **Error** (iter {it + 1}) — `{err}`")
        table = _rows_to_markdown_table(rows)
        return log(f"📊 **Result** — **{row_count}** row(s), {ms} ms\n\n{table}")

    # ── Main agentic loop (single dispatcher call) ─────────────────────────────
    try:
        for event, data in agent_stream(
            provider, model, temperature, messages,
            tool_executor=lambda sql: run_tool_sync("run_query", {"sql": sql}),
            enable_thinking=enable_thinking,
            num_ctx=num_ctx,
        ):
            collected_events.append((event, data))

            if event == "stopped":
                got_content = True
                history[-1][1] = "*Stopped.*"
                trace_md = log("⛔ **Stopped by user.**")
                display_history = _history_with_duration(history, answer_started)
                yield display_history, history[:-1], trace_md, "", gr.update(visible=False), _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note
                return

            elif event == "ping":
                yield history, history, trace_md, "", gr.update(), _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note

            elif event == "status":
                history[-1][1] = f"*{data}*"
                if not _first_status_seen:
                    _first_status_seen = True
                    _q = user_message[:120] + ("…" if len(user_message) > 120 else "")
                    trace_md = log(f"⏳ **Processing query using model {model}:** {_q}")
                elif data not in ("Thinking…", "Formulating…", "Formulating query…", "Reasoning…"):
                    trace_md = log(f"⏳ **{data}**")
                yield history, history, trace_md, "", gr.update(), _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note

            elif event == "context_update":
                ctx_bar_html = _make_ctx_bar(data["used"], ctx_limit)
                yield history, history, trace_md, "", gr.update(), _NO_HL, ctx_bar_html, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note

            elif event == "thinking":
                got_content = True
                # Escape < / > (mermaid <<stereotypes>>, --|> arrows) so the
                # raw reasoning is displayed as literal text.
                trace_md = log(f"🧠 **Model reasoning:**\n\n> {_esc_angle(data.strip())}")
                yield history, history, trace_md, "", gr.update(), _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note

            elif event == "thinking_token":
                got_content = True
                trace_md = _stream_thinking(data)
                yield history, history, trace_md, "", gr.update(), _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note

            elif event == "tool_call":
                got_content = True
                _thinking_stream_idx = None
                sql = data.get("args", {}).get("sql", "")
                _pending_sql = sql
                it = data.get("iteration", 0)
                trace_md = _render_tool_call(sql, it)
                history[-1][1] = (
                    (accumulated + " *(running query…)*") if accumulated else "*Running query…*"
                )
                yield history, history, trace_md, "", gr.update(), _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note

            elif event == "tool_result":
                trace_md = _render_tool_result(data)
                if not data.get("error"):
                    new_cache = _build_tool_cache(
                        sql=_pending_sql,
                        all_rows=data.get("all_rows") or data.get("preview_rows") or [],
                        row_count=int(data.get("row_count") or 0),
                    )
                    if new_cache is not None:
                        cache_out = new_cache
                yield history, history, trace_md, "", gr.update(), _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note

            else:  # "final"
                got_content = True
                accumulated += data
                history[-1][1] = accumulated + " ▌"
                yield history, history, trace_md, "", gr.update(), _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note

    except Exception as exc:
        import traceback
        print(f"[chat] agent_stream raised {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        trace_md = log(f"❌ **Exception:** `{type(exc).__name__}: {exc}`")
        _exc_name = type(exc).__name__
        _exc_str = str(exc).lower()
        _is_auth = (
            _exc_name in ("AuthenticationError", "PermissionDeniedError", "UnauthorizedError")
            or "401" in str(exc)
            or "authentication" in _exc_str
            or "invalid_api_key" in _exc_str
            or "api key" in _exc_str
        )
        if _is_auth:
            _key_var = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY" if provider == "openai" else "API key"
            history[-1][1] = (
                f"**Authentication failed.** Your `{_key_var}` appears to be invalid or missing. "
                f"Check the `.env` file and restart the server."
            )
        else:
            history[-1][1] = f"**Error `{_exc_name}`:** {exc}"
        # This turn is kept in `history` (as an error message) but produced no
        # usable reasoning — append a placeholder so reasoning_history stays
        # index-aligned with history turn-for-turn.
        reasoning_history = reasoning_history + [None]
        display_history = _history_with_duration(history, answer_started)
        yield display_history, history, trace_md, "", gr.update(visible=False), _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note
        return

    # ── Compute highlight payload and deliver final answer ─────────────────────
    if ENABLE_VIZ:
        highlight_payload = extract_highlight_payload(collected_events)
        if not highlight_payload["buildings"]:
            ids = fallback_regex_extract(accumulated)
            if ids:
                centroid = _compute_centroid_wgs84(ids)
                highlight_payload = {
                    "buildings": [{"gmlid": g} for g in ids],
                    "centroid": [{"lat": centroid[0], "long": centroid[1]}] if centroid else None,
                }
    else:
        highlight_payload = _NO_HL

    # Track this turn's raw reasoning (Ollama only) so it can optionally be
    # replayed into context on later turns. Always append something (even
    # None) so reasoning_history stays index-aligned with history turn-for-turn.
    turn_transcript = _build_reasoning_transcript(collected_events) if provider == "ollama" else ""
    reasoning_history = reasoning_history + [turn_transcript or None]

    # Self-summarized "lessons learned" (Ollama only). Runs synchronously —
    # adds one extra blocking generation on top of this turn. Best-effort:
    # keeps the previous note if this turn produced nothing new, but always
    # logs the outcome so the toggle's effect is visible either way.
    if add_lessons and provider == "ollama" and turn_transcript:
        trace_md = log(f"Begin of summarizing function!\n\n")
        summary, summary_error = _summarize_reasoning_ollama(turn_transcript, model, num_ctx)
        if summary:
            lessons_note = summary
            quoted = summary.replace("\n", "\n> ")
            trace_md = log(f"📝 **Summary of what I learned:**\n\n> {quoted}")
        else:
            trace_md = log(f"📝 **Lessons-learned summary skipped:** {summary_error}")

    trace_md = log("✅ **Final answer delivered**")
    history[-1][1] = accumulated
    updated_log_history = list(log_history) + [{"query": user_message, "trace": trace_md}]
    display_history = _history_with_duration(history, answer_started)
    _n = len(updated_log_history)
    yield (
      display_history, history, trace_md, "",
        gr.update(visible=False),
        highlight_payload, ctx_bar_html,
        updated_log_history,
        1,
        _log_nav_html(1, _n),
        gr.update(interactive=_n > 1),
        gr.update(interactive=False),
        cache_out,
        reasoning_history,
        lessons_note,
    )
    print(f"[chat] done. final response length={len(accumulated)}", flush=True)


# ── Provider helpers ───────────────────────────────────────────────────────────

def on_provider_change(provider: str) -> tuple:
    models = models_for_provider(provider)
    default = models[0] if models else ""
    is_ollama = provider == "ollama"
    openai_ollama = provider == "openai" and bool(get_openai_ollama_models())
    warn = ""
    if is_ollama and not models:
        warn = "No models found — is OLLAMA_BASE_URL set and reachable?"
    elif is_ollama and default:
        # The model dropdown is about to be reset to `default`, so classify
        # that — not the stale model of the previous provider.
        warn = profile_for_model(default).warning
    info = (
        "Detected Ollama through OPENAI_BASE_URL; models were downloaded from the Ollama server."
        if openai_ollama
        else "Type a custom model name when using OPENAI_BASE_URL to point at vLLM, llama.cpp, or another OpenAI-compatible server."
    )
    return (
        gr.update(choices=models, value=default, info=info),
        gr.update(visible=is_ollama or openai_ollama),
        gr.update(visible=is_ollama, value=warn),
        gr.update(),                            # prompt_mode_radio: unchanged (stays "auto")
        gr.update(visible=is_ollama),           # num_ctx_dropdown: only for local
        gr.update(visible=is_ollama),           # reasoning_replay_checkbox: only for local
        gr.update(visible=is_ollama),           # lessons_checkbox: only for local
    )


def refresh_provider_models(provider: str) -> gr.update:
    models = models_for_provider(provider)
    default = models[0] if models else ""
    openai_ollama = provider == "openai" and bool(get_openai_ollama_models())
    info = (
        "Detected Ollama through OPENAI_BASE_URL; models were downloaded from the Ollama server."
        if openai_ollama
        else "Type a custom model name when using OPENAI_BASE_URL to point at vLLM, llama.cpp, or another OpenAI-compatible server."
    )
    return gr.update(choices=models, value=default, info=info)


# ── Import tab (fullstack only) ────────────────────────────────────────────────

def build_import_tab(
    reload_tiles_state: gr.State | None = None,
    msg_input: gr.Textbox | None = None,
    send_btn: gr.Button | None = None,
) -> None:
    from webui.importer import import_city_file, list_gml_files, run_tiler

    with gr.Tab("Import CityGML / CityJSON"):
        gr.Markdown("### Import a CityGML or CityJSON file into 3DCityDB")
        gr.Markdown(
            "Place your file in `./production/data/`, then select it below and click **Import**.  \n"
            "Supported formats: `.gml`, `.xml` (CityGML) · `.json`, `.jsonl` (CityJSON) · `.gz`, `.gzip`, `.zip` (compressed)"
        )
        with gr.Row():
            file_dropdown = gr.Dropdown(
                choices=list_gml_files(),
                label="City model file",
                info="Files in ./production/data/",
                scale=4,
            )
            refresh_files_btn = gr.Button("Refresh", scale=1, size="sm")

        format_radio = gr.Radio(
            choices=["auto", "citygml", "cityjson"],
            value="auto",
            label="Format",
            info="Auto-detect works for most files. Override for plain .zip archives whose format cannot be inferred from the filename.",
        )

        auto_tile_checkbox = gr.Checkbox(
            label="Generate 3D tiles after import",
            value=ENABLE_VIZ,
            visible=ENABLE_VIZ,
            info="Required to see new buildings in the 3D View. "
                 "Adds 1–10 minutes depending on dataset size.",
        )

        import_btn = gr.Button("Import", variant="primary")
        import_log = gr.Textbox(
            label="Import log",
            lines=20,
            max_lines=40,
            interactive=False,
            show_copy_button=True,
        )

        refresh_files_btn.click(
            fn=lambda: gr.update(choices=list_gml_files()),
            outputs=file_dropdown,
        )

        tiling_done = reload_tiles_state is not None
        extra_outputs = [reload_tiles_state] if tiling_done else []
        lock_chat = msg_input is not None and send_btn is not None

        def run_import_and_tile(filename: str, fmt_override: str, auto_tile: bool, current_reload: int = 0):
            def _pack(log_val: str, reload_val: int, finished: bool):
                out = [log_val]
                if tiling_done:
                    out.append(reload_val)
                if lock_chat:
                    chat_update = gr.update(interactive=finished)
                    out.extend([chat_update, chat_update])
                return tuple(out) if len(out) > 1 else out[0]

            log = ""
            import_succeeded = False
            no_change = current_reload

            for line in import_city_file(filename, fmt_override):
                log += line
                if "Import finished successfully" in line:
                    import_succeeded = True
                yield _pack(log, no_change, False)

            if not import_succeeded and ENABLE_VIZ:
                log += "\nSkipping tile generation because import did not succeed.\n"
                yield _pack(log, no_change, True)
                return

            try:
                _refresh_system_prompt()
                log += "\n✓ Agent knowledge base refreshed.\n"
                yield _pack(log, no_change, False)
            except Exception as exc:
                log += f"\n⚠ Could not refresh agent knowledge base: {exc}\n"
                yield _pack(log, no_change, True)
                return

            if not auto_tile or not ENABLE_VIZ:
                yield _pack(log, no_change, True)
                return

            log += "\n" + "═" * 60 + "\n"
            log += "Starting 3D tile generation...\n"
            log += "═" * 60 + "\n"
            yield _pack(log, no_change, False)

            for line in run_tiler():
                log += line
                yield _pack(log, no_change, False)

            log += "\n✓ 3D tiles ready — the 3D viewer is reloading the tileset.\n"
            new_reload = current_reload + 1
            yield _pack(log, new_reload, True)

        _import_event = import_btn.click
        if lock_chat:
            _import_event = import_btn.click(
                fn=lambda: (gr.update(interactive=False), gr.update(interactive=False)),
                outputs=[msg_input, send_btn],
            ).then

        _import_event(
            fn=run_import_and_tile,
            inputs=[file_dropdown, format_radio, auto_tile_checkbox] + ([reload_tiles_state] if tiling_done else []),
            outputs=[import_log] + extra_outputs + ([msg_input, send_btn] if lock_chat else []),
        )


# ── Main UI ────────────────────────────────────────────────────────────────────

# Print / PDF export (🖨 button → window.print).
# Verified against the Gradio 5.50 DOM: a single .message-wrap wraps the whole
# conversation; each bubble is a .message-row.user-row / .message-row.bot-row
# (role classes .user / .bot live on inner divs); math renders via KaTeX
# (span.katex / span.katex-display); mermaid renders to <svg> inside .prose.
# IMPORTANT: this must NOT go into gr.Blocks(css=...) — Gradio rewrites every
# selector to be scoped under .gradio-container .contain and drops @page rules,
# which breaks printing for elements outside that scope (footer, body::before).
# It is injected at runtime as an unscoped <style> element by _RESIZE_JS.
_PRINT_CSS = """
@media print {
  @page { size: A4; margin: 15mm 12mm; }

  html, body {
    background: #ffffff !important;
    color: #1e293b !important;
  }
  .gradio-container {
    background: #ffffff !important;
    color: #1e293b !important;
    max-width: 100% !important;
  }

  /* Hide all UI chrome: header, settings sidebar, tabs, trace,
     Cesium viewer, input row, per-message buttons, footer. */
  .header-bar,
  #top-row > .column:first-child,
  #top-row > .splitter,
  #chat-row > .column:nth-of-type(2),
  #chat-row > .splitter,
  #chat-row > .column:first-child > .row:last-child,
  .tab-container,
  .tab-wrapper,
  #agent-trace,
  .log-nav-header,
  .log-nav-row,
  .message-buttons,
  .message-buttons-left,
  .message-buttons-right,
  .mermaid-toolbar,
  /* chatbot block corner UI: floating "Chatbot" label + icon buttons (trash) */
  .wrapper:has([aria-label="chatbot conversation"]) > label,
  .wrapper:has([aria-label="chatbot conversation"]) > .icon-button-wrapper,
  footer,
  iframe {
    display: none !important;
  }

  /* Chat column expands to full page width */
  #top-row, #chat-row { display: block !important; }
  #top-row > .column:last-of-type,
  #chat-row > .column:first-child {
    width: 100% !important;
    max-width: 100% !important;
    min-width: 0 !important;
    flex: none !important;
  }

  /* Kill every viewport/fixed-height constraint so content flows across pages.
     Gradio pins fixed heights all the way down the chain (gradio-container,
     main.fillable, the chatbot .block with overflow:auto, the 500px log
     "wrapper" div) — if any of them keeps its viewport height, print output
     is clipped to the first screenful and shows a scrollbar. */
  body, main, .gradio-container, .contain, .wrap,
  [aria-label="chatbot conversation"],
  :has([aria-label="chatbot conversation"]) {
    height: auto !important;
    max-height: none !important;
  }
  /* Gradio lays the whole app out with nested flex (body > gradio-app >
     .gradio-container > main > .wrap > ...). Chrome paginates through flex
     containers fine, but Firefox keeps the first page almost empty because
     the flex chain is treated as one unsplittable flow. Force plain block
     flow everywhere in print so text starts directly under the title. */
  body,
  .gradio-container,
  main,
  main > .gradio-container,
  .contain,
  .wrap,
  :has([aria-label="chatbot conversation"]) {
    display: block !important;
  }
  [aria-label="chatbot conversation"],
  .wrapper:has([aria-label="chatbot conversation"]),
  .block:has([aria-label="chatbot conversation"]) {
    overflow: visible !important;
  }

  /* Chatbot area: no scroll, no fixed height, no border */
  .bubble-wrap, .message-wrap {
    height: auto !important;
    min-height: 0 !important;
    max-height: none !important;
    overflow: visible !important;
    border: none !important;
    border-radius: 0 !important;
    padding: 0 !important;
  }

  /* Bubbles: full width, question vs. answer clearly separated.
     Verified real Gradio 5.50 DOM: ONE .message-wrap wraps the whole
     conversation; the role lives on each .message-row.user-row /
     .message-row.bot-row (with .user / .bot divs inside). */
  .message-wrap .message-row,
  .message-row { display: block !important; }
  .message-row.user-row,
  .message-row.bot-row,
  .message-wrap.user,
  .message-wrap.bot {
    width: 100% !important;
    max-width: 100% !important;
    margin: 0 0 12px 0 !important;
    padding: 10px 14px !important;
    border-radius: 6px !important;
    text-align: left !important;
    box-shadow: none !important;
  }
  /* Neutralize Gradio's inner bubble styling so the row IS the bubble */
  .message-row > .flex-wrap,
  .message-row .message {
    background: transparent !important;
    border: none !important;
    padding: 0 !important;
    margin: 0 !important;
    border-radius: 0 !important;
  }
  .message-row.user-row,
  .message-wrap.user {
    background: #eef2f7 !important;
    border: 1px solid #cbd5e1 !important;
  }
  .message-row.user-row::before,
  .message-wrap.user::before {
    content: "❓ User" !important;
    display: block !important;
    font-size: 0.75rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: #0f766e;
    margin-bottom: 6px;
  }
  .message-row.bot-row,
  .message-wrap.bot {
    background: #ffffff !important;
    border: 1px solid #e2e8f0 !important;
  }
  .message-row.bot-row::before,
  .message-wrap.bot::before {
    content: "🤖 Assistant (" attr(data-model) ")" !important;
    display: block !important;
    font-size: 0.75rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: #0369a1;
    margin-bottom: 6px;
  }

  /* Markdown content: print-safe light colors (also for dark mode) */
  .message-wrap .prose,
  .message-wrap .prose p,
  .message-wrap .prose li,
  .message-wrap .prose td { color: #1e293b !important; }
  .message-wrap .prose h1, .message-wrap .prose h2,
  .message-wrap .prose h3, .message-wrap .prose h4,
  .message-wrap .prose h5 { color: #0f172a !important; }
  .message-wrap .prose a { color: #0369a1 !important; }
  .answer-duration {
    font-size: 0.78em !important;
    color: #64748b !important;
  }
  .message-wrap .prose code {
    background: #e2e8f0 !important; color: #374151 !important;
    border-radius: 3px; padding: 1px 4px; font-size: 0.85em;
  }
  .message-wrap .prose pre {
    background: #f1f5f9 !important;
    border: 1px solid #cbd5e1 !important;
    border-radius: 4px;
    padding: 10px 12px;
    overflow: visible !important;
    white-space: pre-wrap !important;
    word-break: break-word !important;
    break-inside: avoid;
    page-break-inside: avoid;
  }
  .message-wrap .prose pre code {
    background: transparent !important; padding: 0; color: #1e293b !important;
  }
  .message-wrap .prose table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
  .message-wrap .prose th, .message-wrap .prose td {
    border: 1px solid #cbd5e1; padding: 4px 8px;
  }
  .message-wrap .prose th { background: #e2e8f0 !important; font-weight: 600; }
  .message-wrap .prose blockquote {
    border-left: 3px solid #93c5fd; color: #64748b !important;
  }
  .message-wrap .prose img { max-width: 100%; }
  .message-wrap .prose hr { border: none; border-top: 1px solid #cbd5e1; }

  /* Math (KaTeX): never split across pages */
  .katex { break-inside: avoid; page-break-inside: avoid; }
  .katex-display {
    break-inside: avoid;
    page-break-inside: avoid;
    margin: 12px 0 !important;
    overflow: hidden !important;
  }

  /* Mermaid diagrams (rendered to <svg> by the markdown renderer) */
  .message-wrap .prose svg {
    max-width: 100% !important;
    height: auto !important;
    break-inside: avoid;
    page-break-inside: avoid;
    display: block;
    margin: 10px auto;
  }

  /* Static export header */
  body::before {
    content: attr(data-print-title);
    display: block;
    width: 100%;
    font-size: 1.05rem;
    font-weight: 600;
    color: #0f172a;
    border-bottom: 2px solid #334155;
    padding-bottom: 6px;
    margin-top: 8px;
    margin-bottom: 14px;
  }
}
"""


# Client-side drag-to-resize for the column rows marked `resizable-row`.
# Injected via gr.Blocks(js=...) because gr.HTML sanitizes its content and
# would strip <script> tags. Gradio expects a function expression of the form
# "async (_win, _event_data) => { ... }" — wrapping this as plain statements
# crashes the Svelte mount and breaks the whole UI. Do NOT name a parameter
# `window`: it would shadow the global `window` and arrive undefined.
# Runs once per page load;
# polls briefly because tab contents may render a tick after initial page load.
_RESIZE_JS = """
async (_win, _event_data) => {
  if (window.__citydbSplitsInit) return;
  window.__citydbSplitsInit = true;

  var SPECS = [
    { id: "top-row",  storage: "citydb.split.top",  minLeft: 200, minRight: 320 },
    { id: "chat-row", storage: "citydb.split.chat", minLeft: 320, minRight: 260 }
  ];

  function isCol(el) {
    return el.classList.contains("column") ||
      el.classList.contains("col") ||
      el.classList.contains("gr-column");
  }

  function setupRow(spec) {
    var row = document.getElementById(spec.id);
    if (!row || row.__splitterSet) return;

    // Resolve the columns live on every call: Svelte replaces the column
    // elements on re-renders (tab switches, streaming updates), so a one-time
    // closure capture would point at a detached node and style changes would
    // have no visible effect.
    function getCols() {
      var cs = [];
      for (var i = 0; i < row.children.length; i++) {
        var el = row.children[i];
        if (el !== row.querySelector(".splitter") && isCol(el)) cs.push(el);
      }
      return { left: cs[0], right: cs[1] };
    }
    function hasCols() {
      var n = 0;
      for (var i = 0; i < row.children.length; i++) {
        if (isCol(row.children[i])) n++;
      }
      return n >= 2;
    }
    if (!hasCols()) return;  // columns not rendered yet — poll() retries

    row.__splitterSet = true;
    var handle = document.createElement("div");
    handle.className = "splitter";
    row.appendChild(handle);

    // Factory inline style captured from the fresh columns (Gradio:
    // "flex-grow: 1; min-width: min(220px, 100%)") — used by the dblclick
    // reset.
    var factoryStyle = getCols().left.getAttribute("style");

    function position() {
      var c = getCols();
      if (c.left) handle.style.left = c.left.getBoundingClientRect().width + "px";
    }
    function clamp(w) {
      var maxW = row.getBoundingClientRect().width - spec.minRight - 12;
      return Math.max(spec.minLeft, Math.min(w, maxW));
    }
    function apply(w) {
      // gr.Row is a flexbox (not grid): pin the left column to a fixed width,
      // let the right column absorb the remaining space.
      var c = getCols();
      if (!c.left || !c.right) return;
      c.left.style.flex = "0 0 " + w + "px";
      c.left.style.width = w + "px";
      c.left.style.maxWidth = w + "px";
      c.right.style.flex = "1 1 0px";
      c.right.style.width = "auto";
      c.right.style.maxWidth = "none";
      position();
    }

    // Restore previous split (survives reloads)
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(spec.storage) || "null"); } catch (e) {}
    if (saved && typeof saved.w === "number") apply(clamp(saved.w));
    else position();

    handle.addEventListener("mousedown", function (e) {
      e.preventDefault();
      var c0 = getCols();
      if (!c0.left) return;
      var startX = e.clientX, startW = c0.left.getBoundingClientRect().width, lastW = startW;
      handle.classList.add("active");
      document.body.classList.add("splitting");
      function onMove(ev) {
        lastW = clamp(startW + (ev.clientX - startX));
        apply(lastW);
      }
      function onUp() {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        handle.classList.remove("active");
        document.body.classList.remove("splitting");
        try { localStorage.setItem(spec.storage, JSON.stringify({ w: lastW })); } catch (e) {}
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });

    // Double-click resets to the default ratio
    handle.addEventListener("dblclick", function () {
      var c = getCols();
      if (!c.left || !c.right) return;
      c.left.style.cssText = factoryStyle || "";
      c.right.style.cssText = factoryStyle || "";
      try { localStorage.removeItem(spec.storage); } catch (e) {}
      position();
    });

    var rt = null;
    window.addEventListener("resize", function () {
      clearTimeout(rt);
      rt = setTimeout(function () {
        var c = getCols();
        if (c.left) apply(clamp(c.left.getBoundingClientRect().width));
      }, 120);
    });
  }

  // Rows may render a tick later (tab lazy rendering) — retry briefly.
  var tries = 0;
  (function poll() {
    var pending = SPECS.some(function (s) { return !document.getElementById(s.id); });
    SPECS.forEach(setupRow);
    if (pending && ++tries < 100) setTimeout(poll, 200);
  })();

  // Gradio rebuilds the chatbot DOM when a follow-up query starts. Keep
  // completed answer timings display-only and restore them after that rebuild
  // without adding them to the Python conversation history.
  (function watchAnswerDurations() {
    var timings = window._answerDurations || [];
    window._answerDurations = timings;
    var timingRe = /Time to answer the question [0-9]+[.][0-9]{3} seconds/;
    function normalize(text) {
      return text.replace(timingRe, "").replace(/ +/g, " ")
        .split(String.fromCharCode(10)).join(" ")
        .split(String.fromCharCode(13)).join(" ")
        .split(String.fromCharCode(9)).join(" ").trim();
    }
    function scan() {
      document.querySelectorAll(".message-row.bot-row .prose, .message-wrap.bot .prose").forEach(function (prose) {
        var durationNode = Array.from(prose.querySelectorAll(".answer-duration"))[0];
        if (!durationNode) {
          Array.from(prose.querySelectorAll("p")).some(function (paragraph) {
            if (timingRe.test(paragraph.textContent || "")) {
              paragraph.classList.add("answer-duration");
              durationNode = paragraph;
              return true;
            }
            return false;
          });
        }
        var base = normalize(prose.textContent || "");
        if (durationNode) {
          var timing = durationNode.textContent.trim();
          if (!timings.some(function (item) { return item.base === base; })) {
            timings.push({ base: base, timing: timing });
          }
        } else {
          var saved = timings.find(function (item) { return item.base === base; });
          if (saved) {
            var paragraph = document.createElement("p");
            paragraph.className = "answer-duration";
            var emphasis = document.createElement("em");
            emphasis.textContent = saved.timing;
            paragraph.appendChild(emphasis);
            prose.appendChild(paragraph);
          }
        }
      });
    }
    var observer = new MutationObserver(function () { scan(); });
    observer.observe(document.body, { childList: true, subtree: true });
    scan();
  })();

  // ── PDF export via the native browser print dialog ─────────────────────
  // Inject the print CSS as an UNSCOPED <style> element at runtime.
  // Gradio rewrites every gr.Blocks(css=...) selector to be scoped under
  // ".gradio-container .contain" and drops @page rules — that breaks printing
  // for elements outside that scope (the footer, the body::before header).
  // The _RESIZE_JS string is passed through gr.Blocks(js=...) untouched, so a
  // style tag added here keeps @page and matches document-level elements.
  if (!document.getElementById("citydb-print-css")) {
    var st = document.createElement("style");
    st.id = "citydb-print-css";
    st.textContent = __PRINT_CSS__;
    document.head.appendChild(st);
  }

  // ── No math rendering in the "Agent activity" trace panel ────────────
  // Gradio's markdown renderer applies KaTeX page-wide, which would mangle
  // TeX snippets inside SQL / agent output. Keep formulas there as raw
  // text: whenever a rendered .katex / .katex-display node appears in
  // #agent-trace, replace it with its TeX source (the annotation element
  // inside the (visually hidden) MathML carries it verbatim). The observer
  // is recursive — Svelte swaps the #agent-trace element itself on
  // re-renders (tab switches, streaming updates), so each fresh element
  // gets its own observer.
  var traceObservers = new WeakSet();
  function rawTex(katexEl) {
    var ann = katexEl.querySelector("annotation[encoding='application/x-tex']");
    if (ann && ann.textContent) return ann.textContent;
    var text = (katexEl.textContent || "").replace(/\\s+/g, " ").trim();
    return (text ? "\\(" + text + "\\)" : "(formula)");
  }
  function demoteMathInTrace(root) {
    var done = [];
    for (var i = 0; i < root.childNodes.length; i++) {
      var n = root.childNodes[i];
      if (n.nodeType !== 1) continue;
      var k = n.classList && (n.classList.contains("katex") ||
        n.classList.contains("katex-display")) ? n
        : n.querySelector ? n.querySelector(".katex, .katex-display") : null;
      if (!k) continue;
      var code = document.createElement("code");
      code.textContent = (k.classList.contains("katex-display") ? "$$" : "$") +
        rawTex(k) + (k.classList.contains("katex-display") ? "$$" : "$");
      k.replaceWith(code);
      done.push(code);
    }
    return done;
  }
  function watchTraceMath(el) {
    if (!el || traceObservers.has(el)) return;
    traceObservers.add(el);
    var ob = new MutationObserver(function (muts) {
      muts.forEach(function (m) {
        m.addedNodes.forEach(function (n) {
          if (n.nodeType !== 1 || !n.querySelector) return;
          if (n.classList && (n.classList.contains("katex") ||
              n.classList.contains("katex-display"))) {
            if (n.parentNode) demoteMathInTrace(n.parentNode);
          } else if (n.querySelector(".katex, .katex-display")) {
            demoteMathInTrace(n);  // katex node(s) nested in the added subtree
          }
        });
      });
    });
    ob.observe(el, { childList: true, subtree: true });
  }
  (function watchTracePoll() {
    var t = document.getElementById("agent-trace");
    if (t) {
      watchTraceMath(t);
      return; // element exists — observer now handles all streaming updates
    }
    setTimeout(watchTracePoll, 200);
  })();

  // ── Mermaid error boxes: show raw source + error message ─────────────
  // Gradio's markdown renderer calls mermaid.run() without error handling:
  // on invalid syntax Mermaid replaces the .mermaid node's content with an
  // error SVG (aria-roledescription="error", "Syntax error in text" +
  // "mermaid version X.Y.Z"). The generated source survives only in the
  // error SVG, and the on-screen message is generic.
  // Strategy: (1) as soon as a .mermaid node is added, recover its COMPLETE
  // source — the HTML parser corrupts stereotypes like "<<abstract>>" before
  // mermaid sees them — and remember it (before mermaid.run mutates the node),
  // (2) when the error SVG appears, replace the node with a descriptive
  // message and the raw source in <pre><code> so the user can read and copy it.
  //
  // Recover the exact mermaid source for a .mermaid node from the message
  // bubble's aria-label. Gradio's markdown renderer injects the fenced source
  // UNESCAPED into <div class="mermaid">, so a classDiagram stereotype such as
  // "<<abstract>>" is parsed by the HTML parser as a bogus <abstract> element
  // and the COMPLETE source never reaches the DOM node — mermaid then renders
  // (or errors on) the corrupted fragment. But the bubble element carries the
  // RAW message markdown in its aria-label ("bot's message: ```mermaid …```"),
  // uncorrupted. We extract the matching fenced block, keyed by the node's
  // position among the bubble's .mermaid nodes, and return it (or null if the
  // node has no labelled bubble, e.g. inside the agent-trace panel).
  function mermaidSourceFromLabel(node) {
    var el = node;
    while (el && el !== document.body) {
      var label = el.getAttribute ? el.getAttribute("aria-label") : null;
      if (label && /'s message:/.test(label)) {
        var fences = [];
        var re = /```mermaid\\s*\\n([\\s\\S]*?)```/g;
        var m;
        while ((m = re.exec(label)) !== null) fences.push(m[1].trim());
        if (!fences.length) return null;
        var all = el.querySelectorAll ? el.querySelectorAll(".mermaid") : [];
        var idx = 0;
        for (var k = 0; k < all.length; k++) { if (all[k] === node) { idx = k; break; } }
        return fences[idx] != null ? fences[idx] : fences[0];
      }
      el = el.parentElement;
    }
    return null;
  }
  // mermaid 11.x's run() pipeline (the path Gradio uses to render ```mermaid```
  // blocks) reads the node's innerHTML, entity-decodes it, then runs it through
  // ts-dedent, which strips the MINIMUM common leading indent. For a
  // classDiagram whose body lines are all indented >= 4 (the usual case), that
  // strips 4 spaces from every line, pushing a struct's closing brace to column
  // 0. mermaid 11's class lexer rejects an EMPTY struct whose closing } sits at
  // column 0 ("Expecting 'MEMBER', got 'STRUCT_STOP'"), so an otherwise-valid
  // diagram — one that parses fine via parse()/render() and renders on external
  // mermaid sites — fails ONLY through run(). Prepending a column-0 "%%" line
  // forces ts-dedent's min-indent to 0 (the %% line itself sits at column 0),
  // making the transform a no-op so run() sees the original indentation. "%%" is
  // a valid, inert comment in class diagrams only, so gate on that type.
  function neutralizeDedent(src) {
    var t = (src == null) ? "" : String(src);
    if (/^\\s*classDiagram\\b/i.test(t)) return "%%\\n" + t;
    return t;
  }
  // Concrete parse error for a source string: import the SAME mermaid module
  // Gradio's Vite build serves (assets/mermaid.core-<hash>.js, already cached
  // in the browser's module registry) and call mermaid.parse() — it rejects
  // with the Jison message, e.g.
  //   "Parse error on line 4: … Expecting 'STRUCT_STOP', 'MEMBER', …"
  // (Gradio's renderer calls mermaid.run() WITHOUT a catch, so that rejection
  // message is lost; the rendered error SVG only says "Syntax error in text").
  // The hashed module URL is read from the Performance Resource Timing API —
  // Gradio loads mermaid as an ESM import, so no <script> tag exposes it.
  function mermaidParseError(source) {
    return new Promise(function (resolve) {
      var done = false;
      function finish(d) { if (!done) { done = true; resolve(d); } }
      try {
        var s = String(source || "");
        if (!s.trim()) return finish("");
        // Drop the column-0 "%%" line neutralizeDedent() may have prepended —
        // it is an inert comment and not part of the generated source.
        s = s.replace(/^%%[^\\n]*\\n/, "");
        var src = null;
        var entries = performance.getEntriesByType("resource");
        for (var i = 0; i < entries.length; i++) {
          if (entries[i].name.indexOf("mermaid.core") !== -1) {
            src = entries[i].name;
            break;
          }
        }
        if (!src) return finish("");
        import(src).then(function (mod) {
          for (var k in mod) {
            var v = mod[k];
            if (v && v.default && typeof v.default.parse === "function") {
              var api = v.default;
              api.parse(s)
                .then(function () { finish(""); })
                .catch(function (e) {
                  finish(String((e && e.message) || e || "").trim());
                });
              return;
            }
          }
          finish("");
        }).catch(function () { finish(""); });
      } catch (e) { finish(""); }
    });
  }
  function mermaidFallback(mermaidNode, errSvg) {
    var source = (mermaidNode && mermaidNode.__mermaidSource != null)
      ? mermaidNode.__mermaidSource
      : "";
    var wrap = document.createElement("div");
    wrap.className = "mermaid-error-fallback";
    var msg = document.createElement("p");
    msg.className = "mermaid-fallback-msg";
    // Backup: read the error SVG's <text> nodes ("Syntax error in text"
    // header and "mermaid version X.Y.Z" are skipped — mermaid 11 does not
    // embed the parse error in the SVG, but keep this for other versions).
    function readErrDetails(svg) {
      var d = "";
      if (!svg || !svg.querySelectorAll) return d;
      var ts = svg.querySelectorAll("text");
      for (var i = 0; i < ts.length; i++) {
        var s = (ts[i].textContent || "").trim();
        if (!s) continue;
        if (/^syntax error in text$/i.test(s)) continue;
        if (/^mermaid version /i.test(s)) continue;
        d += (d ? " — " : "") + s;
      }
      return d.replace(/\\s+/g, " ").trim();
    }
    var GENERIC = "⚠️ Mermaid syntax error — the diagram could not be rendered. " +
      "The generated source is shown below; copy and fix it.";
    msg.textContent = GENERIC;
    var pre = document.createElement("pre");
    var code = document.createElement("code");
    code.textContent = source || "(source not available)";
    pre.appendChild(code);
    wrap.appendChild(msg);
    wrap.appendChild(pre);
    if (mermaidNode && mermaidNode.parentNode) mermaidNode.replaceWith(wrap);
    // Upgrade the generic message with the concrete parse error (async — the
    // mermaid module is fetched by the browser, parse() rejects with the
    // Jison message). Falls back to the generic message if anything fails.
    var d = readErrDetails(errSvg);
    if (d) msg.textContent = "⚠️ Mermaid syntax error: " + d;
    mermaidParseError(source).then(function (detail) {
      if (detail && wrap.isConnected) {
        msg.textContent = "⚠️ Mermaid syntax error: " + detail;
      }
    });
  }
  // ── Mermaid copy toolbar: [⧉ SVG] [🖼 PNG] [</> Code] above every diagram ──
  // [⧉ SVG]  → serializes the rendered <svg> (with xmlns) and writes it to the
  //            clipboard as image/svg+xml + text/plain, so it can be pasted
  //            into PowerPoint / other programs as a vector graphic.
  //            Hidden on browsers without SVG clipboard support (Firefox).
  // [🖼 PNG]  → rasterizes the SVG to a 2x PNG (white bg) + text/plain; works
  //            everywhere, including Firefox.
  // [</> Code] → copies the raw mermaid text (node.__mermaidSource, captured
  //            before mermaid mutated the node — without the dedent "%%" prefix)
  //            for pasting into an editor or an external mermaid renderer.
  // [🖼 PNG] → rasterized PNG copy (2x), works on ALL browsers incl. Firefox.
  // On browsers without image/svg+xml clipboard support (Firefox, all
  // versions) the SVG button is hidden entirely — the PNG button covers it.
  function svgClipboardSupported() {
    return !!(window.ClipboardItem && ClipboardItem.supports &&
              ClipboardItem.supports("image/svg+xml"));
  }
  function mermaidCopyButtons(node) {
    var svg = node.querySelector("svg:not([aria-roledescription='error'])");
    if (!svg || node.querySelector(".mermaid-toolbar")) return;
    var bar = document.createElement("div");
    bar.className = "mermaid-toolbar";
    function mkBtn(label, title, kind) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "mermaid-tool-btn";
      b.textContent = label;
      b.title = title;
      b.setAttribute("data-mermaid-copy", kind);
      bar.appendChild(b);
      return b;
    }
    var svgBtn = mkBtn("⧉ SVG",
      "Copy the rendered diagram as an SVG vector graphic to the clipboard. " +
      "Paste it into PowerPoint, Word, draw.io, Inkscape, Figma, etc. — it stays " +
      "sharp at any zoom level and text remains editable where supported.",
      "svg");
    mkBtn("🖼 PNG",
      "Copy the rendered diagram as a rasterized PNG image (2× resolution, white " +
      "background) to the clipboard. Paste it anywhere an image can be pasted — " +
      "email, chat, slides, documents. Works in every browser, including Firefox.",
      "png");
    mkBtn("</> Code",
      "Copy the original Mermaid source text (unrendered) to the clipboard. " +
      "Paste it into an editor, share it, or render it with any Mermaid tool " +
      "(mermaid.live, GitHub, Obsidian, ...).",
      "code");
    if (!svgClipboardSupported()) svgBtn.style.display = "none";
    svg.parentNode.insertBefore(bar, svg);
  }
  // Click handling is done via ONE delegated listener on document, not per
  // button: Gradio re-renders the chat message on each streaming chunk and
  // Svelte can swap the button's inner DOM nodes, which drops any listener
  // attached directly to a button. A document-level listener survives all of
  // that and resolves the LIVE .mermaid node / SVG at click time.
  (function mermaidCopyDelegate() {
    if (document.__mermaidCopyDelegate) return;
    document.__mermaidCopyDelegate = true;
    function flash(btn, ok) {
      if (!btn) return;
      var old = btn.textContent;
      btn.textContent = ok ? "✓" : "✗";
      setTimeout(function () { btn.textContent = old; }, 1200);
    }
    // Serialize the diagram as a SELF-CONTAINED standalone SVG.
    // Critical: mermaid keeps ALL diagram styling (fonts, node/edge classes,
    // marker colors) in a separate <style> tag in the document — selectors are
    // prefixed with the svg's id — NOT inside the <svg> element itself. A
    // plain cloneNode() export therefore loses every style: text falls back
    // to serif, edges/misdrawn splines, illegible labels. So we:
    //   1. clone the svg,
    //   2. pin explicit pixel width/height from the viewBox (the live svg
    //      uses width="100%"),
    //   3. embed the matching document <style> block inside the clone.
    function serializeStandaloneSvg(svg) {
      var vb = (svg.getAttribute("viewBox") || "").trim().split(/\s+/).map(Number);
      var clone = svg.cloneNode(true);
      clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
      if (vb.length === 4 && vb[2] > 0 && vb[3] > 0) {
        clone.setAttribute("width", String(vb[2]));
        clone.setAttribute("height", String(vb[3]));
      }
      var id = svg.id;
      if (id) {
        var styleEl = null;
        var allStyles = document.querySelectorAll("style");
        for (var i = 0; i < allStyles.length; i++) {
          if (allStyles[i].textContent.indexOf("#" + id) !== -1) {
            styleEl = allStyles[i];
            break;
          }
        }
        if (styleEl) {
          var st = document.createElementNS("http://www.w3.org/2000/svg", "style");
          st.textContent = styleEl.textContent;
          clone.insertBefore(st, clone.firstChild);
        }
      }
      return new XMLSerializer().serializeToString(clone);
    }
    // Rasterize the SVG to a 2x PNG blob and write it to the clipboard.
    // Needed because Firefox (all versions) does NOT support image/svg+xml
    // clipboard items — the whole clipboard.write() promise rejects and the
    // clipboard is left unchanged. Chrome/Edge/Opera accept SVG natively.
    // `text` must be the SELF-CONTAINED serialization from
    // serializeStandaloneSvg() (styles embedded) or the image loses all
    // mermaid styling.
    function writePngToClipboard(svg, text) {
      var vb = (svg.getAttribute("viewBox") || "").trim().split(/\s+/).map(Number);
      var vw = (vb.length === 4 && vb[2] > 0) ? vb[2] : svg.clientWidth;
      var vh = (vb.length === 4 && vb[3] > 0) ? vb[3] : svg.clientHeight;
      if (!vw || !vh) return Promise.reject(new Error("Cannot determine SVG size"));
      var scale = 2;
      var c = document.createElement("canvas");
      c.width = Math.round(vw * scale);
      c.height = Math.round(vh * scale);
      var ctx = c.getContext("2d");
      var img = new Image();
      return new Promise(function (resolve, reject) {
        img.onload = function () {
          ctx.fillStyle = "#ffffff";
          ctx.fillRect(0, 0, c.width, c.height);
          ctx.drawImage(img, 0, 0, c.width, c.height);
          c.toBlob(function (blob) {
            if (!blob) return reject(new Error("PNG rasterization failed"));
            navigator.clipboard.write([
              new ClipboardItem({
                "image/png":    blob,
                "text/plain":   new Blob([text], { type: "text/plain" })
              })
            ]).then(resolve, reject);
          }, "image/png");
        };
        img.onerror = function () { reject(new Error("SVG rasterization failed")); };
        img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(text);
      });
    }
    function copySvg(mNode) {
      var svg = mNode.querySelector("svg:not([aria-roledescription='error'])");
      if (!svg) return null;
      var text = serializeStandaloneSvg(svg);
      // Very old browsers without async clipboard.write(): best-effort text.
      if (!navigator.clipboard || !navigator.clipboard.write) {
        return navigator.clipboard
          ? navigator.clipboard.writeText(text)
          : null;
      }
      var svgWrite = navigator.clipboard.write([
        new ClipboardItem({
          "image/svg+xml": new Blob([text], { type: "image/svg+xml" }),
          "text/plain":    new Blob([text], { type: "text/plain" })
        })
      ]);
      if (!window.ClipboardItem || !ClipboardItem.supports) {
        // No supports() probe (older Chrome): try SVG, fall back to PNG if
        // the write is rejected.
        return svgWrite.catch(function (err) {
          try { return writePngToClipboard(svg, text); } catch (e) { throw err; }
        });
      }
      if (!ClipboardItem.supports("image/svg+xml")) {
        return writePngToClipboard(svg, text); // safety net; button is hidden
      }
      return svgWrite;
    }
    function copyPng(mNode) {
      var svg = mNode.querySelector("svg:not([aria-roledescription='error'])");
      if (!svg) return null;
      var text = serializeStandaloneSvg(svg);
      return writePngToClipboard(svg, text);
    }
    document.addEventListener("click", function (e) {
      var btn = e.target && e.target.closest ? e.target.closest(".mermaid-tool-btn") : null;
      if (!btn) return;
      var bar = btn.closest(".mermaid-toolbar");
      if (!bar) return;
      // The toolbar div is always a direct child of the .mermaid node, but
      // resolve defensively in case Gradio ever wraps it.
      var mNode = bar.closest(".mermaid") || bar.parentElement;
      if (!mNode) return;
      e.preventDefault();
      e.stopPropagation();
      var kind = btn.getAttribute("data-mermaid-copy");
      var p;
      try {
        p = kind === "code"
          ? navigator.clipboard.writeText(
              mNode.__mermaidSource != null ? mNode.__mermaidSource : "")
          : kind === "png"
            ? copyPng(mNode)
            : copySvg(mNode);
      } catch (err) { flash(btn, false); return; }
      if (p && p.then) {
        p.then(function () { flash(btn, true); },
               function () { flash(btn, false); });
      } else {
        flash(btn, true);
      }
    }, false);
  })();
  (function watchMermaidErrors() {
    var ob = new MutationObserver(function (muts) {
      muts.forEach(function (m) {
        m.addedNodes.forEach(function (n) {
          if (n.nodeType !== 1) return;
          // mermaid.run() mutates the .mermaid node's children in place,
          // so on later mutations the .mermaid node is an ANCESTOR of the
          // added node — check itself, ancestors, and descendants.
          var found = [];
          var anc = n;
          while (anc && anc.nodeType === 1) {
            if (anc.classList && anc.classList.contains("mermaid")) found.push(anc);
            anc = anc.parentElement;
          }
          if (n.querySelectorAll) {
            var desc = n.querySelectorAll(".mermaid");
            for (var j = 0; j < desc.length; j++) found.push(desc[j]);
          }
          for (var i = 0; i < found.length; i++) {
            var node = found[i];
            if (node.__mermaidFallbackDone) continue;
            // Capture the raw source on the node's first appearance, before
            // mermaid.run() mutates it. The canonical source comes from the
            // message bubble's aria-label (see mermaidSourceFromLabel); the
            // DOM text is only a fallback because the HTML parser corrupts
            // any "<<stereotype>>" before mermaid ever sees the node.
            if (!node.__mermaidSource &&
                !node.querySelector("svg, [id^='dmermaid'], .error-text")) {
              var full = mermaidSourceFromLabel(node);
              if (full != null) {
                // Overwrite the corrupted text synchronously so that the
                // upcoming mermaid.run() (a later microtask/rAF) reads the
                // complete source and renders the real diagram.
                // neutralizeDedent() prepends a column-0 "%%" anchor to
                // classDiagram sources so run()'s ts-dedent step becomes a
                // no-op (see that helper for the full rationale).
                node.textContent = neutralizeDedent(full);
                node.__mermaidSource = full.trim();
              } else {
                // No labelled bubble (agent-trace): best effort — unwrap the
                // bogus "<<stereotype>>" tags the parser created. Each bogus
                // element stands in for a literal "<name>" token; replace it
                // with that text and hoist its (text) children back into
                // place. Uppercase in the stereotype is unrecoverable (the
                // parser lowercases it).
                var bogus = node.querySelectorAll ? node.querySelectorAll("*") : [];
                for (var b = bogus.length - 1; b >= 0; b--) {
                  var el = bogus[b];
                  var p = el.parentNode;
                  p.insertBefore(
                    document.createTextNode("<" + el.tagName.toLowerCase() + ">"),
                    el
                  );
                  while (el.firstChild) p.insertBefore(el.firstChild, el);
                  el.remove();
                }
                node.__mermaidSource = (node.textContent || "").trim();
                // Same classDiagram dedent guard as the labelled path above.
                node.textContent = neutralizeDedent(node.__mermaidSource);
              }
            }
            // Keep mermaid as plain text in the "Agent activity" trace panel
            // (same rationale as the KaTeX demotion above): once mermaid has
            // started rendering (an svg, dmermaid wrapper, or error marker is
            // present), swap the whole node for its captured source in a
            // <pre>. The chatbot messages keep the rendered diagram.
            if (node.closest && node.closest("#agent-trace") &&
                node.querySelector("svg, [id^='dmermaid'], .error-text")) {
              node.__mermaidFallbackDone = true;
              var pre = document.createElement("pre");
              var code = document.createElement("code");
              code.textContent = (node.__mermaidSource != null)
                ? node.__mermaidSource : "(source not available)";
              pre.appendChild(code);
              if (node.parentNode) node.replaceWith(pre);
              continue;
            }
            // Mermaid 11 marks the error svg via aria-roledescription, but it
            // sets that attribute AFTER the childList mutations — so detect the
            // error through its childList-visible children instead. The
            // message is in the .error-text <text> elements; wait for those
            // before replacing (an .error-icon alone only arms the detection,
            // the texts arrive in a later mutation batch).
            var errText = node.querySelector(".error-text");
            var errAttr = node.querySelector('svg[aria-roledescription="error"]');
            if (errText || errAttr) {
              node.__mermaidFallbackDone = true;
              var mark = errText || errAttr;
              var errSvg = mark.closest ? mark.closest("svg") : (mark.ownerSVGElement || mark);
              mermaidFallback(node, errSvg);
            }
            // Success path: rendering finished with a real <svg> → attach the
            // copy toolbar right above it. Skip error nodes (detected through
            // their childList-visible markers, see above), the agent-trace
            // panel (kept as raw text), and double-insertion.
            if (!node.__mermaidButtonsDone &&
                !node.querySelector(".error-text, .error-icon, svg[aria-roledescription='error']") &&
                node.querySelector("svg") &&
                !node.querySelector(".mermaid-toolbar") &&
                !(node.closest && node.closest("#agent-trace"))) {
              node.__mermaidButtonsDone = true;
              mermaidCopyButtons(node);
            }
          }
        });
      });
    });
    ob.observe(document.body, { childList: true, subtree: true });
  })();

  // window.print() renders whatever is already in the DOM — Gradio's
  // markdown renderer (KaTeX for $$…$$, mermaid for ```mermaid``` blocks)
  // has produced the final HTML/SVG, so the print CSS (see _PRINT_CSS above)
  // is all that is needed. A short delay lets a still-streaming answer settle
  // before the print dialog opens.
  (function () {
    var printButton = document.getElementById("export-pdf-btn");
    if (printButton) {
      var hint = "Print conversation (or save as PDF)";
      printButton.title = hint;
      var innerButton = printButton.querySelector("button");
      if (innerButton) innerButton.title = hint;
    }
  })();

  window._exportPdf = function () {
    var now = new Date();
    // Date getters and toLocaleTimeString() use the browser's local timezone.
    var printDateTime = now.getFullYear() + "-" +
      String(now.getMonth() + 1).padStart(2, "0") + "-" +
      String(now.getDate()).padStart(2, "0") + ", " +
      now.toLocaleTimeString();
    document.body.setAttribute(
      "data-print-title",
      "3DCityDB-MCP — Chat-Export — " + printDateTime
    );
    var modelControl = document.querySelector("#model-dropdown input") ||
      document.querySelector("#model-dropdown [role='combobox']");
    var model = modelControl && modelControl.value ? modelControl.value : "unknown model";
    document.querySelectorAll(".message-row.bot-row, .message-wrap.bot").forEach(function (node) {
      node.setAttribute("data-model", model);
    });
    var prev = document.title;
    document.title = "3DCityDB-Chat-" + new Date().toISOString().slice(0, 10);
    setTimeout(function () {
      window.print();
      // Restore after the print dialog closed (some browsers fire
      // afterprint; do it both ways to be safe).
      document.title = prev;
    }, 300);
  };
  window.addEventListener("afterprint", function () {
    document.title = "3DCityDB-MCP";
    document.body.removeAttribute("data-print-title");
  });
}
""".replace("__PRINT_CSS__", json.dumps(_PRINT_CSS))


def build_ui() -> gr.Blocks:
    detected_provider = detect_default_provider()
    initial_provider = detected_provider or "anthropic"
    initial_models = models_for_provider(initial_provider)
    initial_model = initial_models[0] if initial_models else ""
    no_provider = detected_provider is None
    initial_is_ollama = initial_provider == "ollama"
    initial_openai_ollama = initial_provider == "openai" and bool(get_openai_ollama_models())
    if initial_is_ollama and not initial_models:
        initial_dynamic_warn = "No models found — is OLLAMA_BASE_URL set and reachable?"
    elif initial_is_ollama and initial_model:
        initial_dynamic_warn = profile_for_model(initial_model).warning
    else:
        initial_dynamic_warn = ""
    initial_model_info = (
      "Detected Ollama through OPENAI_BASE_URL; models were downloaded from the Ollama server."
      if initial_openai_ollama
      else "Type a custom model name when using OPENAI_BASE_URL to point at vLLM, llama.cpp, or another OpenAI-compatible server."
    )

    with gr.Blocks(
        title="3DCityDB-MCP",
        theme=gr.themes.Soft(
            primary_hue="slate",
            font=[gr.themes.GoogleFont("Open Sans"), "ui-sans-serif", "sans-serif"],
            font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "monospace"],
        ),
        js=_RESIZE_JS,
        css="""
        .header-bar { background: #1e293b; padding: 16px 24px; border-radius: 8px; margin-bottom: 8px; }
        .header-bar h1 { color: #f8fafc; margin: 0; font-size: 1.4rem; }
        .header-bar p  { color: #94a3b8; margin: 4px 0 0; font-size: 0.85rem; }
        #send-btn, #stop-btn { min-width: 48px !important; width: 48px !important; padding: 0 !important; font-size: 1.1rem !important; }
        #set-temperature-checkbox {
          align-self: center !important;
          display: flex !important;
          align-items: center !important;
        }
        #set-temperature-checkbox label {
          white-space: nowrap !important;
          align-items: center !important;
          margin: 0 !important;
        }
        .temperature-row,
        #set-temperature-checkbox,
        #set-temperature-checkbox .wrap {
          overflow: visible !important;
          background: transparent !important;
          background-color: transparent !important;
          background-image: none !important;
          border: 0 !important;
          box-shadow: none !important;
          padding-top: 0 !important;
          padding-bottom: 0 !important;
          min-height: 0 !important;
        }
        #set-temperature-checkbox * {
          background: transparent !important;
          background-color: transparent !important;
          background-image: none !important;
        }
        .temperature-row { flex-wrap: nowrap !important; }
        .temperature-row > div:first-child {
          background: transparent !important;
          background-color: transparent !important;
          background-image: none !important;
          border: 0 !important;
          box-shadow: none !important;
        }
        .temperature-row > div:first-child * {
          background: transparent !important;
          background-color: transparent !important;
          background-image: none !important;
          border-color: transparent !important;
          box-shadow: none !important;
        }
        .temperature-row input[type="number"] {
          background: #ffffff !important;
          border: 1px solid #d1d5db !important;
          border-radius: 8px !important;
          box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05) !important;
        }
        #set-temperature-checkbox input[type="checkbox"] {
          appearance: auto !important;
          width: 16px !important;
          height: 16px !important;
          margin: 0 8px 0 0 !important;
          border: 1px solid #cbd5e1 !important;
          border-radius: 4px !important;
          background: #ffffff !important;
          accent-color: #2563eb !important;
        }
        .answer-duration { font-size: 0.78em !important; color: #64748b; }

        /* ── Agent trace: light mode (default) ───────────────────── */
        #agent-trace {
            max-height: """ + ("300px" if ENABLE_VIZ else "600px") + """; overflow-y: auto;
            font-size: 0.82rem; line-height: 1.6;
            background: #f8fafc;
            border: 1px solid #cbd5e1;
            border-radius: 6px; padding: 10px 14px;
            color: #1e293b;
            font-family: "JetBrains Mono", "Fira Code", ui-monospace, monospace;
        }
        #agent-trace strong { color: #0369a1; }
        #agent-trace code { background: #e2e8f0; color: #374151; border-radius: 3px; padding: 1px 4px; font-size: 0.78rem; }
        #agent-trace pre {
            background: #f1f5f9 !important;
            border: 1px solid #cbd5e1;
            border-radius: 4px; padding: 8px 12px; overflow-x: auto; color: #1e293b;
        }
        #agent-trace pre code { background: transparent; padding: 0; color: inherit; }
        #agent-trace table { border-collapse: collapse; width: 100%; margin-top: 4px; }
        #agent-trace th { background: #e2e8f0; color: #0369a1; text-align: left; padding: 4px 8px; border: 1px solid #cbd5e1; font-size: 0.78rem; }
        #agent-trace td { padding: 3px 8px; border: 1px solid #e2e8f0; color: #374151; font-size: 0.78rem; }
        #agent-trace tr:nth-child(even) td { background: #f1f5f9; }
        #agent-trace tr:nth-child(odd)  td { background: #ffffff; }
        #agent-trace hr { border: none; border-top: 1px solid #e2e8f0; margin: 8px 0; }
        #agent-trace blockquote { border-left: 3px solid #93c5fd; margin: 4px 0; padding: 4px 10px; color: #64748b; }
        #agent-trace::-webkit-scrollbar { width: 6px; }
        #agent-trace::-webkit-scrollbar-track { background: #f1f5f9; }
        #agent-trace::-webkit-scrollbar-thumb { background: #94a3b8; border-radius: 3px; }
        .trace-panel-label { font-weight: 600; font-size: 0.9rem; color: #0369a1; }

        /* ── Agent trace: dark mode (Gradio adds .dark to <html>) ── */
        .dark #agent-trace {
            background: #0f172a;
            border-color: #1e293b;
            color: #cbd5e1;
        }
        .dark #agent-trace strong { color: #7dd3fc; }
        .dark #agent-trace code { background: #1e293b; color: #94a3b8; }
        .dark #agent-trace pre { background: #1e293b !important; border-color: #334155; color: #e2e8f0; }
        .dark #agent-trace th { background: #1e293b; color: #7dd3fc; border-color: #334155; }
        .dark #agent-trace td { border-color: #1e293b; color: #cbd5e1; }
        .dark #agent-trace tr:nth-child(even) td { background: #0f172a; }
        .dark #agent-trace tr:nth-child(odd)  td { background: #111827; }
        .dark #agent-trace hr { border-top-color: #1e293b; }
        .dark #agent-trace blockquote { border-left-color: #334155; color: #94a3b8; }
        .dark #agent-trace::-webkit-scrollbar-track { background: #0f172a; }
        .dark #agent-trace::-webkit-scrollbar-thumb { background: #334155; }
        .dark .trace-panel-label { color: #7dd3fc; }

        /* ── Log nav (above trace title) ─────────────────────────── */
        .log-nav-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:4px; }
        .log-nav-header .trace-panel-label { margin:0; }
        .log-nav-controls { display:flex; align-items:center; gap:4px; }
        .log-nav-btn { min-width:32px !important; max-width:32px !important; padding:0 !important; height:28px !important; font-size:0.9rem !important; }

        /* ── Resizable column sections ─────────────────────────── */
        .resizable-row { position: relative; }
        .resizable-row > div { min-width: 0 !important; }
        .splitter {
            position: absolute; top: 0; width: 8px; height: 100%;
            transform: translateX(-50%);
            cursor: col-resize; z-index: 30;
        }
        .splitter::after {
            content: ""; display: block; width: 2px; height: 100%;
            margin: 0 auto; background: transparent;
            transition: background 0.15s ease;
        }
        .splitter:hover::after, .splitter.active::after { background: #3b82f6; }
        body.splitting, body.splitting * { cursor: col-resize !important; user-select: none !important; }
        body.splitting iframe { pointer-events: none; }
        /* ── Mermaid error fallback (injected by _RESIZE_JS) ────────── */
        .mermaid-error-fallback { border: 1px solid #fca5a5; background: #fef2f2; border-radius: 6px; padding: 10px 12px; margin: 10px 0; }
        .mermaid-error-fallback .mermaid-fallback-msg { margin: 0 0 8px 0; font-size: 0.85rem; font-weight: 600; color: #b91c1c; }
        .mermaid-error-fallback pre { margin: 0; white-space: pre-wrap; word-break: break-word; background: #fff; border: 1px solid #fecaca; border-radius: 4px; padding: 8px; font-size: 0.8rem; }
        .dark .mermaid-error-fallback { background: #450a0a; border-color: #7f1d1d; }
        .dark .mermaid-error-fallback .mermaid-fallback-msg { color: #fca5a5; }
        .dark .mermaid-error-fallback pre { background: #1e293b; border-color: #7f1d1d; color: #e2e8f0; }

        /* ── Mermaid copy toolbar (inserted by _RESIZE_JS) ─────────── */
        .mermaid-toolbar { display: flex; justify-content: flex-end; gap: 4px; margin: 0 0 4px 0; }
        .mermaid-tool-btn {
            border: 1px solid #cbd5e1; background: #f8fafc; color: #475569;
            border-radius: 4px; font-size: 0.72rem; line-height: 1;
            padding: 3px 8px; cursor: pointer;
        }
        .mermaid-tool-btn:hover { background: #e2e8f0; border-color: #94a3b8; }
        .dark .mermaid-tool-btn { background: #1e293b; color: #cbd5e1; border-color: #334155; }
        .dark .mermaid-tool-btn:hover { background: #334155; }

        /* NOTE: the @media print / PDF-export styles are injected at runtime
           as an unscoped <style> element via _RESIZE_JS (see _PRINT_CSS above).
           Gradio's gr.Blocks(css=...) rewrites every selector to be scoped
           under .gradio-container .contain and drops @page rules, which would
           break printing of elements outside that scope (footer, body::before). */
        """,
    ) as demo:

        with gr.Row(elem_classes="header-bar"):
            gr.HTML(
                "<h1>3DCityDB-MCP</h1>"
                "<p>Natural-language interface for 3DCityDB v5 &nbsp;&middot;&nbsp; "
                f"{'Fullstack' if VARIANT == 'fullstack' else 'BYOD'} mode</p>"
            )

        if no_provider:
            gr.HTML(
                '<div style="background:#fee2e2;border:1px solid #fca5a5;border-radius:8px;'
                'padding:12px 16px;margin-bottom:8px;color:#991b1b;font-size:0.9rem;">'
                "<strong>No LLM provider configured.</strong> "
                "Set at least one of <code>ANTHROPIC_API_KEY</code>, <code>OPENAI_API_KEY</code> "
                "(or <code>OPENAI_BASE_URL</code> for a local OpenAI-compatible server), "
                "or <code>OLLAMA_BASE_URL</code> (reachable) "
                "in your <code>.env</code> file and restart the server."
                "</div>"
            )

        reload_tiles_state = gr.State(0) if ENABLE_VIZ else None

        with gr.Row(elem_classes="resizable-row", elem_id="top-row"):

            # ── Sidebar ───────────────────────────────────────────────────────
            with gr.Column(scale=1, min_width=220):
                gr.Markdown("### Settings")
                provider_radio = gr.Radio(
                    choices=["anthropic", "openai", "ollama"],
                    value=initial_provider,
                    label="Provider",
                )
                model_dropdown = gr.Dropdown(
                    choices=initial_models,
                    value=initial_model,
                    label="Model",
                  elem_id="model-dropdown",
                    allow_custom_value=True,
                    info=initial_model_info,
                )
                dynamic_warn = gr.Markdown(
                    visible=initial_is_ollama,
                    value=initial_dynamic_warn,
                )
                refresh_ollama_btn = gr.Button(
                  "Refresh models", size="sm", visible=initial_is_ollama or initial_openai_ollama
                )
                with gr.Row(equal_height=True, elem_classes="temperature-row"):
                    set_temperature_checkbox = gr.Checkbox(
                        label="Set temperature",
                        value=False,
                    scale=0,
                      min_width=150,
                      elem_id="set-temperature-checkbox",
                    )
                    temperature_slider = gr.Number(
                      minimum=0.0, maximum=1.0, step=0.05,
                      value=0.1, label="", show_label=False, interactive=False,
                      scale=0,
                      min_width=100,
                    )
                thinking_dropdown = gr.Dropdown(
                    choices=["off", "low", "medium", "high"],
                    value="off",
                    label="Thinking",
                  info="Reasoning level for thinking-capable models: Ollama uses "
                    "think; OpenAI-compatible endpoints use reasoning_effort. "
                    "off / low / medium / high. Slower but more thorough.",
                )
                prompt_mode_radio = gr.Radio(
                    choices=["auto", "compact", "full"],
                    value="auto",
                    label="Prompt mode",
                    info="Auto picks compact for small local models. Override for complex queries.",
                )
                num_ctx_dropdown = gr.Dropdown(
                    choices=_CTX_OPTIONS,
                    value=_CTX_DEFAULT,
                    label="Context window (Ollama)",
                    visible=initial_is_ollama,
                    info="Tokens available to the model. 128K recommended for complex queries.",
                )
                reasoning_replay_checkbox = gr.Checkbox(
                    label="Include all reasoning steps in context (Ollama)",
                    value=True,
                    visible=initial_is_ollama,
                    info="Feeds each turn's full Thought/Action/Observation trace back into "
                         "context on later turns. Increases token usage significantly.",
                )
                lessons_checkbox = gr.Checkbox(
                    label="Add self-summarized \"lessons learned\" (Ollama)",
                    value=False,
                    visible=initial_is_ollama,
                    info="After each turn, asks the model to summarize what it learned and "
                         "carries that note forward. Adds one extra blocking generation per turn.",
                )
                reset_btn = gr.Button("New conversation", size="sm")
                context_bar = gr.HTML(
                    value="",
                    elem_id="context-bar",
                )
                gr.Markdown("---")
                status_bar = gr.HTML(value=get_status_html(initial_provider, initial_model))
                gr.Markdown(
                    "**API keys** are read from environment variables:  \n"
                    "`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,  \n"
                    "`OLLAMA_BASE_URL`  \n"
                    "Set `OPENAI_BASE_URL` to route the OpenAI provider to vLLM, "
                    "llama.cpp, or another OpenAI-compatible server."
                )

            # ── Main area ─────────────────────────────────────────────────────
            with gr.Column(scale=4):

                with gr.Tab("Chat"):
                    if ENABLE_VIZ:
                        with gr.Row(equal_height=True, elem_classes="resizable-row", elem_id="chat-row"):
                            with gr.Column(scale=1, min_width=360):
                                db_warning = gr.HTML(value="")
                                chatbot = gr.Chatbot(
                                    height=460,
                                    type="tuples",
                                    render_markdown=True,
                                    sanitize_html=True,
                                    allow_tags=False,
                                    latex_delimiters=[
                                        {"left": "$$", "right": "$$", "display": True},
                                        {"left": "$", "right": "$", "display": False},
                                    ],
                                )
                                with gr.Row():
                                    msg_input = gr.Textbox(
                                        placeholder="Assembling agent context, please wait…",
                                        label="", scale=8, lines=1,
                                        interactive=False,
                                    )
                                    send_btn = gr.Button("➤", variant="primary", scale=0, min_width=48, elem_id="send-btn", interactive=False)
                                    stop_btn = gr.Button("⏹", variant="stop", scale=0, min_width=48, elem_id="stop-btn", visible=False)
                                    export_pdf_btn = gr.Button("🖨", variant="secondary", scale=0, min_width=48, elem_id="export-pdf-btn")

                            with gr.Column(scale=1, min_width=360):
                                gr.HTML(
                                    '<iframe id="cesium-iframe" src="/cesium-viewer/index.html" '
                                    'style="width:100%;height:540px;border:none;border-radius:6px;"></iframe>'
                                )

                        with gr.Row():
                            with gr.Column():
                                gr.HTML(
                                    '<div class="log-nav-header">'
                                    '<span class="trace-panel-label">🔍 Agent activity</span>'
                                    '<span class="log-nav-controls" id="log-nav-placeholder"></span>'
                                    '</div>'
                                )
                                with gr.Row(elem_classes="log-nav-row", visible=True):
                                    log_prev_btn = gr.Button("←", size="sm", min_width=32, interactive=False, elem_classes="log-nav-btn")
                                    log_page_label = gr.HTML(_log_nav_html(0, 0))
                                    log_next_btn = gr.Button("→", size="sm", min_width=32, interactive=False, elem_classes="log-nav-btn")
                                agent_trace = gr.Markdown(
                                    value="*Idle — waiting for first query.*",
                                    elem_id="agent-trace",
                                )

                    else:
                        with gr.Row(equal_height=False, elem_classes="resizable-row", elem_id="chat-row"):
                            with gr.Column(scale=3, min_width=380):
                                db_warning = gr.HTML(value="")
                                chatbot = gr.Chatbot(
                                    height=500,
                                    type="tuples",
                                    render_markdown=True,
                                    sanitize_html=True,
                                    allow_tags=False,
                                    latex_delimiters=[
                                        {"left": "$$", "right": "$$", "display": True},
                                        {"left": "$", "right": "$", "display": False},
                                    ],
                                )
                                with gr.Row():
                                    msg_input = gr.Textbox(
                                        placeholder="Assembling agent context, please wait…",
                                        label="", scale=8, lines=1,
                                        interactive=False,
                                    )
                                    send_btn = gr.Button("➤", variant="primary", scale=0, min_width=48, elem_id="send-btn", interactive=False)
                                    stop_btn = gr.Button("⏹", variant="stop", scale=0, min_width=48, elem_id="stop-btn", visible=False)
                                    export_pdf_btn = gr.Button("🖨", variant="secondary", scale=0, min_width=48, elem_id="export-pdf-btn")

                            with gr.Column(scale=2, min_width=300):
                                gr.HTML(
                                    '<div class="log-nav-header">'
                                    '<span class="trace-panel-label">🔍 Agent activity</span>'
                                    '<span class="log-nav-controls" id="log-nav-placeholder"></span>'
                                    '</div>'
                                )
                                with gr.Row(elem_classes="log-nav-row", visible=True):
                                    log_prev_btn = gr.Button("←", size="sm", min_width=32, interactive=False, elem_classes="log-nav-btn")
                                    log_page_label = gr.HTML(_log_nav_html(0, 0))
                                    log_next_btn = gr.Button("→", size="sm", min_width=32, interactive=False, elem_classes="log-nav-btn")
                                agent_trace = gr.Markdown(
                                    value="*Idle — waiting for first query.*",
                                    elem_id="agent-trace",
                                )

                if VARIANT == "fullstack":
                    build_import_tab(
                        reload_tiles_state if ENABLE_VIZ else None,
                        msg_input=msg_input,
                        send_btn=send_btn,
                    )

                with gr.Tab("MCP Inspector"):
                    gr.Markdown("### Active MCP tools")
                    gr.Markdown(
                        "The MCP server exposes these tools to the agent:\n\n"
                        "| Tool | Description |\n"
                        "|---|---|\n"
                        "| `assemble_prompt` | Builds the full system prompt |\n"
                        "| `run_query` | Read-only SELECT (500 row cap) |\n"
                        "| `scan_objectclasses` | Object class hierarchy |\n"
                        "| `resolve_properties` | Properties per object class |\n"
                        "| `get_generic_attributes` | User-defined attributes |\n"
                        "| `get_db_context_snapshot` | SRS, bbox, feature counts |\n"
                        "| `get_lod_config` | Available LoD levels |\n"
                        "| `get_examples` | Curated SQL examples |\n"
                        "| `get_database_schema` | Table/column definitions |\n"
                        "| `get_query_guidelines` | Indexed columns & best practices |"
                    )
                    with gr.Accordion("Refresh system prompt", open=False):
                        refresh_prompt_btn = gr.Button("Re-assemble system prompt")
                        prompt_status = gr.Textbox(
                            label="Status", interactive=False, lines=1
                        )
                        refresh_prompt_btn.click(
                            fn=lambda: (gr.update(interactive=False), gr.update(interactive=False)),
                            outputs=[msg_input, send_btn],
                        ).then(
                            fn=lambda: (
                                _refresh_system_prompt(),
                                "Done — system prompt refreshed.",
                                gr.update(interactive=True),
                                gr.update(interactive=True),
                            )[-3:],
                            outputs=[prompt_status, msg_input, send_btn],
                        )

                with gr.Tab("System Prompt"):
                    gr.Markdown("### Assembled system prompt")
                    gr.Markdown(
                        "The full prompt sent to the LLM as context — assembled live "
                        "from the MCP server tools (schema, examples, guidelines, …)."
                    )
                    load_prompt_btn = gr.Button("Load / Refresh prompt", variant="secondary")
                    prompt_size_info = gr.Markdown("")
                    prompt_viewer = gr.Textbox(
                        label="",
                        lines=30,
                        max_lines=60,
                        interactive=False,
                        show_copy_button=True,
                        placeholder='Click "Load / Refresh prompt" to view the assembled prompt.',
                    )

                    def _load_and_show_prompt(prompt_mode: str, provider: str, model: str):
                        effective_compact, label = _resolve_compact(prompt_mode, provider, model)
                        sp = _get_system_prompt(compact=effective_compact)
                        full = CHAT_INSTRUCTIONS + "\n\n" + sp
                        size_label = f"**Current prompt:** {len(full):,} chars ({label}, {len(full)//3:,} tokens est.)"
                        return full, size_label

                    load_prompt_btn.click(
                        fn=_load_and_show_prompt,
                        inputs=[prompt_mode_radio, provider_radio, model_dropdown],
                        outputs=[prompt_viewer, prompt_size_info],
                    )

        # ── State ─────────────────────────────────────────────────────────────
        # (Resizable section dividers: script is injected via gr.Blocks(js=_RESIZE_JS)
        # above, targeting the rows marked `resizable-row`.)
        history_state = gr.State([])
        highlight_state = gr.JSON(visible=False, value={"buildings": [], "centroid": None})
        log_history_state = gr.State([])
        log_page_state = gr.State(0)
        # Holds the last successful tool result so the agent can answer
        # follow-up questions without re-querying. See _build_tool_cache.
        tool_cache_state = gr.State(None)
        # Ollama-only: per-turn raw reasoning trace (index-aligned with
        # history_state) and the rolling self-summarized "lessons learned"
        # note. See _build_reasoning_transcript / _summarize_reasoning_ollama.
        reasoning_state = gr.State([])
        lessons_state = gr.State(None)

        if ENABLE_VIZ:
            gr.HTML("""
<script>
window._sendHighlight = function(payload) {
  var iframe = document.getElementById('cesium-iframe');
  if (iframe && iframe.contentWindow) {
    iframe.contentWindow.postMessage(
      { type: payload && payload.buildings && payload.buildings.length > 0
          ? 'highlight' : 'clear',
        buildings: (payload && payload.buildings) || [],
        centroid:  (payload && payload.centroid)  || null },
      '*'
    );
  }
};
window._reloadTiles = function() {
  var iframe = document.getElementById('cesium-iframe');
  if (iframe && iframe.contentWindow) {
    iframe.contentWindow.postMessage({ type: 'reload_tiles' }, '*');
  }
};
</script>
""")
            highlight_state.change(
                fn=None,
                inputs=[highlight_state],
                outputs=[],
                js="(payload) => { window._sendHighlight(payload); }"
            )
            reload_tiles_state.change(
                fn=None,
                inputs=[reload_tiles_state],
                outputs=[],
                js="(_) => { window._reloadTiles(); }"
            )

        send_inputs = [
            msg_input, history_state, provider_radio, model_dropdown,
            set_temperature_checkbox, temperature_slider,
            thinking_dropdown, prompt_mode_radio, num_ctx_dropdown,
            log_history_state, tool_cache_state,
            reasoning_replay_checkbox, lessons_checkbox, reasoning_state, lessons_state,
        ]
        send_outputs = [chatbot, history_state, agent_trace, msg_input, stop_btn, highlight_state, context_bar, log_history_state, log_page_state, log_page_label, log_prev_btn, log_next_btn, tool_cache_state, reasoning_state, lessons_state]

        submit_event = msg_input.submit(fn=chat_stream, inputs=send_inputs, outputs=send_outputs)
        click_event = send_btn.click(fn=chat_stream, inputs=send_inputs, outputs=send_outputs)

        def _on_stop_click():
            print("[app] Stop button clicked", flush=True)
            request_stop()
            return gr.update(visible=False)

        stop_btn.click(fn=_on_stop_click, outputs=[stop_btn], queue=False)

        set_temperature_checkbox.change(
          fn=lambda enabled: gr.update(interactive=enabled),
          inputs=set_temperature_checkbox,
          outputs=temperature_slider,
        )

        # PDF export: pure client-side (window.print + @media print CSS).
        # fn=None + js= means no Python round-trip is needed — same pattern
        # as the highlight_state.change(js=...) wiring below.
        export_pdf_btn.click(
            fn=None, inputs=[], outputs=[], queue=False,
            js="() => { if (window._exportPdf) window._exportPdf(); }",
        )

        provider_radio.change(
            fn=on_provider_change,
            inputs=provider_radio,
            outputs=[
                model_dropdown, refresh_ollama_btn, dynamic_warn, prompt_mode_radio, num_ctx_dropdown,
                reasoning_replay_checkbox, lessons_checkbox,
            ],
        )
        refresh_ollama_btn.click(
          fn=refresh_provider_models,
          inputs=provider_radio,
          outputs=model_dropdown,
        )

        def _update_model_warn(model: str) -> str:
            return profile_for_model(model).warning

        model_dropdown.change(
            fn=_update_model_warn,
            inputs=model_dropdown,
            outputs=dynamic_warn,
        )

        def _update_status(provider: str, model: str, prompt_mode: str) -> str:
            _, label = _resolve_compact(prompt_mode, provider, model)
            return get_status_html(provider, model, prompt_mode_label=label)

        provider_radio.change(
            fn=_update_status,
            inputs=[provider_radio, model_dropdown, prompt_mode_radio],
            outputs=status_bar,
        )
        model_dropdown.change(
            fn=_update_status,
            inputs=[provider_radio, model_dropdown, prompt_mode_radio],
            outputs=status_bar,
        )

        def _on_prompt_mode_change(prompt_mode: str, provider: str, model: str, history: list):
            effective, label = _resolve_compact(prompt_mode, provider, model)
            # Evict + pre-warm the newly selected prompt variant.
            new_key = "compact" if effective else "full"
            with _sp_lock:
                _system_prompt_cache.pop(new_key, None)
            threading.Thread(target=lambda: _get_system_prompt(compact=effective), daemon=True).start()

            # Warn when switching to full on a local model.
            if not effective and provider in _LOCAL_PROVIDERS:
                warning = (
                    f"⚠️ **Prompt mode: {label}.** The full prompt will be used for the "
                    "next query — this is significantly larger and will consume more of the "
                    "context window. Switch back to **auto** or **compact** if the model "
                    "starts ignoring instructions or truncating answers."
                )
                new_history = history + [[None, warning]]
                return gr.update(value=new_history), new_history, get_status_html(provider, model, prompt_mode_label=label)
            return gr.update(), history, get_status_html(provider, model, prompt_mode_label=label)

        prompt_mode_radio.change(
            fn=_on_prompt_mode_change,
            inputs=[prompt_mode_radio, provider_radio, model_dropdown, history_state],
            outputs=[chatbot, history_state, status_bar],
        )

        def navigate_logs(direction: int, log_history: list, log_page: int):
            n = len(log_history)
            if n == 0:
                return gr.update(), _log_nav_html(0, 0), gr.update(interactive=False), gr.update(interactive=False), 0
            new_page = max(1, min(n, log_page + direction))
            entry = log_history[n - new_page]
            return (
                entry["trace"],
                _log_nav_html(new_page, n),
                gr.update(interactive=new_page < n),
                gr.update(interactive=new_page > 1),
                new_page,
            )

        def clear_chat(provider: str = "", model: str = "", prompt_mode: str = "auto", num_ctx_label: str = _CTX_DEFAULT):
            effective_compact, _ = _resolve_compact(prompt_mode, provider, model)
            sp = _get_system_prompt(compact=effective_compact)
            base_msgs = [{"role": "system", "content": CHAT_INSTRUCTIONS + "\n\n" + sp}]
            base_tok = _estimate_tokens(base_msgs)
            num_ctx = _CTX_VALUES.get(num_ctx_label, 32768) if provider in _LOCAL_PROVIDERS else None
            ctx_limit_reset = num_ctx if num_ctx else (_get_provider_ctx_limit(provider, model) if provider else 32768)
            reset_bar = _make_ctx_bar(base_tok, ctx_limit_reset)
            return (
                [],
                [],
                "*Idle — waiting for first query.*",
                "",
                {"buildings": [], "centroid": None},
                reset_bar,
                [],
                0,
                _log_nav_html(0, 0),
                gr.update(interactive=False),
                gr.update(interactive=False),
                None,
                [],
                None,
            )

        reset_btn.click(
            fn=clear_chat,
            inputs=[provider_radio, model_dropdown, prompt_mode_radio, num_ctx_dropdown],
            outputs=[chatbot, history_state, agent_trace, msg_input,
                     highlight_state, context_bar,
                     log_history_state, log_page_state, log_page_label, log_prev_btn, log_next_btn,
                     tool_cache_state, reasoning_state, lessons_state],
        )
        chatbot.clear(
            fn=clear_chat,
            inputs=[provider_radio, model_dropdown, prompt_mode_radio, num_ctx_dropdown],
            outputs=[chatbot, history_state, agent_trace, msg_input,
                     highlight_state, context_bar,
                     log_history_state, log_page_state, log_page_label, log_prev_btn, log_next_btn,
                     tool_cache_state, reasoning_state, lessons_state],
        )

        log_prev_btn.click(
            fn=lambda h, p: navigate_logs(1, h, p),
            inputs=[log_history_state, log_page_state],
            outputs=[agent_trace, log_page_label, log_prev_btn, log_next_btn, log_page_state],
        )
        log_next_btn.click(
            fn=lambda h, p: navigate_logs(-1, h, p),
            inputs=[log_history_state, log_page_state],
            outputs=[agent_trace, log_page_label, log_prev_btn, log_next_btn, log_page_state],
        )

        _ASSEMBLING_HTML = (
            '<div style="display:flex;gap:16px;font-size:0.85rem;padding:6px 0;color:#94a3b8;">'
            "⏳ The context is being assembled, please wait…</div>"
        )

        def _on_load(provider: str, model: str, prompt_mode: str, num_ctx_label: str):
            # Yield an immediate placeholder so the user sees our own message
            # instead of Gradio's generic "Processing" queue indicator while
            # the (potentially slow) initial context assembly finishes.
            yield (
                _ASSEMBLING_HTML,
                gr.update(),
                "",
                gr.update(interactive=False, placeholder="Assembling agent context, please wait…"),
                gr.update(interactive=False),
            )

            _, label = _resolve_compact(prompt_mode, provider, model)
            db_status = _check_db_status()
            # clear_chat() calls _get_system_prompt(), which blocks on _sp_lock
            # until the startup pre-warm thread has finished assembling the
            # context — so by the time we get here it is safe to unlock input.
            context_bar_html = clear_chat(provider, model, prompt_mode, num_ctx_label)[5]
            status_html = get_status_html(provider, model, prompt_mode_label=label, db_status=db_status)
            warning_html = _DB_EMPTY_WARNING if db_status == "empty" else ""
            yield (
                status_html,
                context_bar_html,
                warning_html,
                gr.update(interactive=True, placeholder="Ask about your city model…"),
                gr.update(interactive=True),
            )

        demo.load(
            fn=_on_load,
            inputs=[provider_radio, model_dropdown, prompt_mode_radio, num_ctx_dropdown],
            outputs=[status_bar, context_bar, db_warning, msg_input, send_btn],
        )

    return demo


if __name__ == "__main__":
    # Pre-warm both prompt variants so the first query never waits on the MCP server.
    threading.Thread(target=_get_system_prompt, daemon=True).start()
    threading.Thread(target=lambda: _get_system_prompt(compact=True), daemon=True).start()

    detected = detect_default_provider()

    demo = build_ui()

    if ENABLE_VIZ:
        import uvicorn
        from fastapi import FastAPI
        from fastapi.staticfiles import StaticFiles

        _tiles_dir = os.environ.get("TILES_DIR", "/tiles")
        _viewer_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cesium_viewer")

        fastapi_app = FastAPI()
        if os.path.isdir(_tiles_dir):
            fastapi_app.mount("/tiles", StaticFiles(directory=_tiles_dir), name="tiles")
        else:
            print(f"[viz] WARNING: tiles directory not found at {_tiles_dir} — /tiles will 404", flush=True)
        fastapi_app.mount("/cesium-viewer", StaticFiles(directory=_viewer_dir, html=True), name="cesium-viewer")
        demo.queue()
        fastapi_app = gr.mount_gradio_app(fastapi_app, demo, path="/")

        uvicorn.run(
            fastapi_app,
            host="0.0.0.0",
            port=int(os.environ.get("GRADIO_PORT", "7860")),
        )
    else:
        demo.launch(
            server_name="0.0.0.0",
            server_port=int(os.environ.get("GRADIO_PORT", "7860")),
            show_api=False,
            share=False,
        )
