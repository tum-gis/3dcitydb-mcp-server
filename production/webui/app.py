"""Gradio chat UI for citydb-mcp."""

import os
import re
import threading
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
    get_ollama_models,
    _rows_to_markdown_table,
    extract_highlight_payload, fallback_regex_extract,
    _compute_centroid_wgs84,
    request_stop, _clear_stop, _is_stopped,
    _get_provider_ctx_limit, _estimate_tokens, _log_context_usage,
    _ollama_reachable,
    should_use_compact,
)
from webui.backends import stream as agent_stream
from webui.mcp_client import assemble_system_prompt_sync, run_tool_sync

_LOCAL_PROVIDERS = ("ollama",)

_CTX_OPTIONS = ["8K (8,192)", "32K (32,768)",  "64K (65,536)","128K (131,072)", "256K (262,144)"]
_CTX_VALUES = {"8K (8,192)": 8192, "32K (32,768)": 32768, "64K (65,536)": 65536, "128K (131,072)": 131072, "256K (262,144)": 262144}
_CTX_DEFAULT = "32K (32,768)"

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
        return bool(os.environ.get("OPENAI_API_KEY"))
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
    # auto
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

    # NOTE: went through two failed attempts via langchain_ollama's ChatOllama
    # before landing here — the installed version (0.2.0) has no "think" field
    # at all (confirmed via model_fields), and the /no_think prompt
    # soft-switch didn't stop this model from spending its whole num_predict
    # budget on hidden reasoning either (469 chunks, done_reason=length, zero
    # visible .content both times). Calling Ollama's /api/chat directly with
    # a top-level "think": false is the one place this is guaranteed to reach
    # the actual server option, independent of the LangChain wrapper version.
    import json as _json
    import urllib.request as _urllib

    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
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
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.0,
            "num_predict": 500,
            "num_ctx": num_ctx or 32768,
        },
    }
    try:
        req = _urllib.Request(
            f"{base_url}/api/chat",
            data=_json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = float(os.environ.get("OLLAMA_TIMEOUT", "300"))
        with _urllib.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        text = (data.get("message", {}).get("content") or "").strip()
        if not text:
            done_reason = data.get("done_reason", "unknown")
            return "", f"model returned an empty response (done_reason={done_reason})"
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
    temperature: float,
    enable_thinking: bool,
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

    effective_compact, mode_label = _resolve_compact(prompt_mode, provider, model)
    print(f"[chat] prompt_mode={prompt_mode!r}  effective_compact={effective_compact}  ({mode_label})", flush=True)

    # Resolve num_ctx (only meaningful for local providers)
    num_ctx = _CTX_VALUES.get(num_ctx_label, 32768) if provider in _LOCAL_PROVIDERS else None

    _clear_stop()

    _NO_HL = gr.update()
    _NO_CTX = gr.update()

    if not user_message.strip():
        yield history, history, "*Idle.*", "", gr.update(), _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note
        return

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
        trace_lines[_thinking_stream_idx] += token.replace("\n", "  \n")
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
                yield history, history[:-1], trace_md, "", gr.update(visible=False), _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note
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
                trace_md = log(f"🧠 **Model reasoning:**\n\n> {data.strip()}")
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
        yield history, history, trace_md, "", gr.update(visible=False), _NO_HL, _NO_CTX, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), cache_out, reasoning_history, lessons_note
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
    _n = len(updated_log_history)
    yield (
        history, history, trace_md, "",
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
    warn = ""
    if is_ollama and not models:
        warn = "No models found — is OLLAMA_BASE_URL set and reachable?"
    return (
        gr.update(choices=models, value=default),
        gr.update(visible=is_ollama),
        gr.update(visible=is_ollama, value=warn),
        gr.update(),                            # prompt_mode_radio: unchanged (stays "auto")
        gr.update(visible=is_ollama),           # num_ctx_dropdown: only for local
        gr.update(visible=is_ollama),           # reasoning_replay_checkbox: only for local
        gr.update(visible=is_ollama),           # lessons_checkbox: only for local
    )


def refresh_ollama_models() -> gr.update:
    models = get_ollama_models()
    default = models[0] if models else ""
    return gr.update(choices=models, value=default)


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

def build_ui() -> gr.Blocks:
    detected_provider = detect_default_provider()
    initial_provider = detected_provider or "anthropic"
    initial_models = models_for_provider(initial_provider)
    initial_model = initial_models[0] if initial_models else ""
    no_provider = detected_provider is None
    initial_is_ollama = initial_provider == "ollama"
    initial_dynamic_warn = (
        "No models found — is OLLAMA_BASE_URL set and reachable?"
        if (initial_is_ollama and not initial_models) else ""
    )

    with gr.Blocks(
        title="3DCityDB-MCP",
        theme=gr.themes.Soft(
            primary_hue="slate",
            font=[gr.themes.GoogleFont("Open Sans"), "ui-sans-serif", "sans-serif"],
            font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "monospace"],
        ),
        css="""
        .header-bar { background: #1e293b; padding: 16px 24px; border-radius: 8px; margin-bottom: 8px; }
        .header-bar h1 { color: #f8fafc; margin: 0; font-size: 1.4rem; }
        .header-bar p  { color: #94a3b8; margin: 4px 0 0; font-size: 0.85rem; }
        #send-btn, #stop-btn { min-width: 48px !important; width: 48px !important; padding: 0 !important; font-size: 1.1rem !important; }

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
                "Set at least one of <code>ANTHROPIC_API_KEY</code>, <code>OPENAI_API_KEY</code>, "
                "or <code>OLLAMA_BASE_URL</code> (reachable) "
                "in your <code>.env</code> file and restart the server."
                "</div>"
            )

        reload_tiles_state = gr.State(0) if ENABLE_VIZ else None

        with gr.Row():

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
                )
                dynamic_warn = gr.Markdown(
                    visible=initial_is_ollama,
                    value=initial_dynamic_warn,
                )
                refresh_ollama_btn = gr.Button(
                    "Refresh Ollama models", size="sm", visible=initial_is_ollama
                )
                temperature_slider = gr.Slider(
                    minimum=0.0, maximum=1.0, step=0.05,
                    value=0.1, label="Temperature",
                )
                thinking_toggle = gr.Checkbox(
                    label="Enable thinking",
                    value=False,
                    info="Enable for thinking-capable Ollama models (e.g. Qwen3). Slower but more thorough. Has no effect on OpenAI models.",
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
                    value=False,
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
                    "`OLLAMA_BASE_URL`"
                )

            # ── Main area ─────────────────────────────────────────────────────
            with gr.Column(scale=4):

                with gr.Tab("Chat"):
                    if ENABLE_VIZ:
                        with gr.Row(equal_height=True):
                            with gr.Column(scale=1, min_width=360):
                                db_warning = gr.HTML(value="")
                                chatbot = gr.Chatbot(
                                    height=460,
                                    type="tuples",
                                    render_markdown=True,
                                    sanitize_html=True,
                                    allow_tags=False,
                                )
                                with gr.Row():
                                    msg_input = gr.Textbox(
                                        placeholder="Assembling agent context, please wait…",
                                        label="", scale=8, lines=1,
                                        interactive=False,
                                    )
                                    send_btn = gr.Button("➤", variant="primary", scale=0, min_width=48, elem_id="send-btn", interactive=False)
                                    stop_btn = gr.Button("⏹", variant="stop", scale=0, min_width=48, elem_id="stop-btn", visible=False)

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
                        with gr.Row(equal_height=False):
                            with gr.Column(scale=3, min_width=380):
                                db_warning = gr.HTML(value="")
                                chatbot = gr.Chatbot(
                                    height=500,
                                    type="tuples",
                                    render_markdown=True,
                                    sanitize_html=True,
                                    allow_tags=False,
                                )
                                with gr.Row():
                                    msg_input = gr.Textbox(
                                        placeholder="Assembling agent context, please wait…",
                                        label="", scale=8, lines=1,
                                        interactive=False,
                                    )
                                    send_btn = gr.Button("➤", variant="primary", scale=0, min_width=48, elem_id="send-btn", interactive=False)
                                    stop_btn = gr.Button("⏹", variant="stop", scale=0, min_width=48, elem_id="stop-btn", visible=False)

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
            temperature_slider, thinking_toggle, prompt_mode_radio, num_ctx_dropdown,
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

        provider_radio.change(
            fn=on_provider_change,
            inputs=provider_radio,
            outputs=[
                model_dropdown, refresh_ollama_btn, dynamic_warn, prompt_mode_radio, num_ctx_dropdown,
                reasoning_replay_checkbox, lessons_checkbox,
            ],
        )
        refresh_ollama_btn.click(fn=refresh_ollama_models, outputs=model_dropdown)

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
