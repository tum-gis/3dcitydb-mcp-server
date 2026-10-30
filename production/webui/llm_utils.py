"""Shared LLM provider utilities: stop flag, context estimation, model lists, helpers."""

import concurrent.futures as _cf
import json
import os
import re
import threading
import time
import urllib.request

import litellm

_executor = _cf.ThreadPoolExecutor(max_workers=8)

# Stop handling is per turn: see webui.session.TurnControl.


# ── Context-usage estimation & logging ────────────────────────────────────────

_ollama_ctx_cache: dict[str, int] = {}


def _msg_text(m) -> str:
    """Extract the text of a message that actually goes into the model context:
    the content plus any tool-call arguments (SQL queries make up a significant
    share of the context in tool-calling turns). Works for plain dicts and
    litellm/pydantic message objects."""
    if hasattr(m, "get"):
        try:
            content = m.get("content")
            tcs = m.get("tool_calls") or []
        except Exception:
            content, tcs = str(m or ""), []
    else:
        content, tcs = str(m or ""), []
    out = str(content or "")
    for tc in tcs:
        if hasattr(tc, "function") and tc.function is not None:
            fn = tc.function
            name = str(getattr(fn, "name", "") or "")
            args = getattr(fn, "arguments", None)
        elif isinstance(tc, dict):
            fnd = tc.get("function") or {}
            name = str(fnd.get("name") or "")
            args = fnd.get("arguments")
        else:
            name, args = "", None
        if isinstance(args, (dict, list)):
            args = json.dumps(args)
        out += name + str(args or "")
    return out


def _estimate_tokens(messages: list) -> int:
    """Rough token count using 3 chars ≈ 1 token heuristic.

    Counts message content AND tool-call arguments, and includes replayed
    reasoning transcripts (they are part of the assistant message content by
    the time the messages list is built in app.py)."""
    return sum(len(_msg_text(m)) for m in messages) // 3


def _get_ollama_model_ctx(model: str) -> int:
    """Query Ollama /api/show for the model's native context length. Result is cached."""
    if model in _ollama_ctx_cache:
        return _ollama_ctx_cache[model]
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    try:
        import json as _j
        req = urllib.request.Request(
            f"{base}/api/show",
            data=_j.dumps({"model": model}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _j.loads(resp.read())
        info = data.get("model_info", {})
        ctx = (
            info.get("llama.context_length")
            or info.get("general.context_length")
            or info.get("context_length")
        )
        if ctx:
            _ollama_ctx_cache[model] = int(ctx)
            return int(ctx)
    except Exception:
        pass
    configured = int(os.environ.get("OLLAMA_NUM_CTX", "131072"))
    _ollama_ctx_cache[model] = configured
    return configured


def _get_provider_ctx_limit(provider: str, model: str) -> int:
    """Return the effective context token limit for the given provider/model."""
    if provider == "ollama":
        return _get_ollama_model_ctx(model)
    return 200_000


def _log_context_usage(
    provider: str, model: str, messages: list[dict], num_ctx: int | None = None
) -> tuple[int, int, bool]:
    """Log token usage before a LLM call. Returns (used_tokens, limit_tokens, over_80pct)."""
    system_msgs = [m for m in messages if m.get("role") == "system"]
    other_msgs  = [m for m in messages if m.get("role") != "system"]
    system_tok  = _estimate_tokens(system_msgs)
    history_tok = _estimate_tokens(other_msgs)
    total_tok   = system_tok + history_tok
    limit       = num_ctx if num_ctx else _get_provider_ctx_limit(provider, model)
    pct         = (total_tok / limit * 100) if limit > 0 else 0
    over        = pct > 80
    print(
        f"[ctx] system={system_tok:,} tok  history={history_tok:,} tok  "
        f"total={total_tok:,} / {limit:,} ({pct:.0f}%)"
        + ("  ⚠️ >80% — history will be trimmed" if over else ""),
        flush=True,
    )
    return total_tok, limit, over


# ── Chat instructions ──────────────────────────────────────────────────────────
_CHAT_INSTRUCTIONS_TEMPLATE = """\
You are a SQL query assistant for 3DCityDB v5 — a PostgreSQL-based 3D city model database \
following the CityGML standard. The user is exploring a city dataset and wants \
natural-language answers backed by real data.

**Tool-use discipline:**
- If the user sends a greeting, thank-you, or other non-query message (e.g. "hello", \
"thanks", "what can you do?"), reply conversationally with a Final Answer — do NOT call \
`run_query`. Only call `run_query` when the user is asking about data in the database.
- You have direct database access via `run_query`. For any data question, ALWAYS call it — \
never write SQL in your chat reply expecting the user to run it.
- Never emit a `Final Answer` in a turn where no `Observation` from `run_query` \
exists yet. If you have worked out a SQL query in your thinking, your response \
must end with `Action: run_query` and `Action Input: {"sql": "..."}` (on its \
own line — not the JSON directly after `Action:`) and wait for the \
Observation. Quoting or describing SQL in prose is never a substitute for \
calling the tool, and any number or objectid that does not appear in an \
Observation is a hallucination — omit it.
- The user sees the SQL in the Agent Activity panel. Only paste SQL text in your chat reply, if the user explicitly asks for it.
- When the user asks "show me", "list", "which", or any counting question: \
always write a query that SELECTs `feature.objectid AS objectid` plus relevant name/type columns — \
NEVER use `SELECT COUNT(*) alone` unless the user explicitly asks for a count. \
Your final answer must always include the objectid of each matching feature; \
the UI uses these to highlight objects on a map.
- Property names live in a specific namespace_id — always use the namespace_id given in the schema for each property (schema properties are typically ns:8 or ns:10; only generic attributes are ns:3) — and when the question asks about a grouping entity (street, owner, usage), GROUP BY that entity alone, never by feature.objectid.
- Some properties (e.g. height) are nested containers whose own val_* columns are NULL — if the schema marks a property as ⚠️ NESTED TYPE, you MUST join property→property via parent_id to a child row where name = 'value' and read val_double from the child, never from the parent.
- If a `[VIEWER SELECTION]` system block is present, it lists features the user picked in the 3D view. Demonstratives in their question ("these", "the selected ones", "diese Gebäude", ...) refer to those features; filter on those objectids. The block is absent when nothing is selected — then never claim a selection exists.

**Ambiguous questions (mandatory):**
Many everyday words map to more than one thing in this database. "Area" may be a stored \
attribute (e.g. `grossFloorArea`, `netFloorArea`), a generic attribute, or something \
computed from geometry (`CG_3DArea` on a LoD geometry). "Height", "volume", \
"age", "use" and "address" are the same kind of case.
- Never invent a source. Use only what the assembled schema below actually lists for the \
classes in question.
- 1 to 3 plausible sources → answer with ALL of them in one query and one table, with an \
extra column naming the source of each value (the attribute name, or the SQL expression \
for a computed one). Open with one sentence saying the term was ambiguous and which \
sources you used. The `source` column is additive to RULE 1 below, not a replacement — \
`objectid` and the rows RULE 1 asks for are still required.
- More than 3 plausible sources, or sources that need genuinely different queries \
(different joins, different classes, different LoD) → do not guess. Ask which one they \
mean, as a short bullet list of the available options, and run no query (or just to \
identify the options) this turn.
- Never silently pick one interpretation (if others are available) and present it as "the" answer.

**Rendering of mathematical expressions, diagrams and graphics**
- When you produce mathematical expressions or variables, use TeX notation (e.g., $n$ for inline expressions and $$formula$$ for display-style formulas). 
- For diagrams or graphics use mermaid and put markdown code fences with the appropriate language tag (e.g., ```mermaid).
- The user only sees your Final Answer, never your Thought. Put the complete ```mermaid block inside the Final Answer itself — drafting it in your Thought and writing a placeholder such as "pie chart" in the answer shows the user no chart.

**Language mirroring (CRITICAL):**
Respond in the same language the user used. If German → German. If French → French. \
If English → English. Mirror their formality (du/Sie, tu/vous). Use native technical \
vocabulary (e.g. "Gebäude" not "buildings", "Höhe" not "height" in user-facing prose).

**Output formatting (mandatory rules — follow exactly):**

Every answer to a list/show/which question has TWO parts:
  1. A short prose sentence introducing the result (1 short sentence, in the user's language).
  2. A markdown table with the rows.

RULE 1: If the most recent tool_result
returned MORE THAN ONE ROW, your final answer MUST be:
  - One short introductory sentence summarizing what's shown (e.g. "Hier sind die
    13 Wohngebäude in der Straße Röblingweg mit ihrer jeweiligen Wohnungsanzahl:").
  - Followed by a markdown table with the rows.
The table MUST include the `objectid` column plus all relevant attribute columns
from the result. @@LIST_ROWS@@ Do NOT narrate rows as prose. Do NOT group rows by
shared values and describe them sentence-by-sentence.

@@LIST_COUNT@@

RULE 2: If the result has ZERO rows, answer in prose only (e.g. "No matching
buildings were found." in English, or the equivalent in the user's language).
If the result has exactly ONE row, answer in prose with the data inline
(e.g. "Building DEBY_LOD2_4965683 has 25 units." in English).
Always write in the SAME LANGUAGE the user used — never switch to another language.

RULE 3: If the user explicitly asked "how many" / "wie viele" / "combien" AND
the result is a single COUNT(*) value, answer in prose only with the number.
Example (English): "There are **314** buildings in the database."
Example (German):  "Es gibt **314** Gebäude in der Datenbank."
Use whichever matches the user's language — NEVER use the German form for an
English question or vice versa.

RULE 4: Use **bold** for key numbers/names in prose sentences. Do NOT use bold
inside table cells.

RULE 5: Never paste raw JSON; never paste the SQL – unless the user explicitly asks for it. Never include `<think>` / `<thinking>` blocks in your final answer.

@@LIST_RULE_6@@

RULE 7: Never add a text section like "How I solved it", "verified queries",
"reasoning", or "adapt these queries" to your answer, and never quote, repeat,
or imitate the [AUTO-GENERATED SUMMARY] blocks that follow some earlier
answers. Those blocks were appended by a separate process after the fact and
never shown to the user; reproducing them (or anything like them) in your
answer is always wrong.

Examples of CORRECT formatting:

User: "Show me all residential buildings in Röblingweg."
Tool result: 3 rows with objectid and function
Answer:
The following **3** residential buildings are located in Röblingweg:

| objectid           | function     |
|--------------------|--------------|
| DEBY_LOD2_4965683  | residential  |
| DEBY_LOD2_4965796  | residential  |
| DEBY_LOD2_4965797  | residential  |

**Error handling:**
If a query fails, fix it silently and retry. Do not ask the user for help unless you've \
tried 3 different approaches.

**Honesty:**
If you genuinely cannot answer with the available data, say so directly. Do not invent \
objectclass IDs, property names, or values — use only what's in the assembled schema below.
"""

# How long result tables are written (setting "Show every row in result
# tables", ChatTurnSettings.full_lists). Full: every row, as before the short
# mode existed. Short: 5 example rows above FULL_RESULT_MIN_ROWS; both modes
# attach the complete result as CSV/JSON below the answer (format_full_result).
_LIST_RULES = {
    True: {
        "@@LIST_ROWS@@": """The table MUST contain EVERY row returned by the query — never
omit, filter out, or truncate rows, even if they have NULL values in some columns
(show NULL as an empty cell).""",
        "@@LIST_COUNT@@": """RULE 1b: The count you state in the introductory sentence MUST equal the number
of rows in your table, which MUST equal the number of rows the query returned.
Never write "10 buildings" if the query returned 15 rows.""",
        "@@LIST_RULE_6@@": """RULE 6: Never truncate a table. Never use `...` or `…` or "and X more" to shorten
the table. Every row from the tool result MUST appear as its own table row.
If this makes the answer long — that is correct and expected.""",
    },
    False: {
        "@@LIST_ROWS@@": """Up to 10 rows: the table MUST contain EVERY row returned by the
query — never omit, filter out, or truncate rows, even if they have NULL values in
some columns (show NULL as an empty cell). MORE THAN 10 rows: the table shows only
the FIRST 5 rows, followed by one sentence saying these are 5 of N and that the full
list is attached below the answer (CSV and JSON, ready to copy). The app attaches it
automatically: never type the full list yourself and never run the query again just
to see more rows.""",
        "@@LIST_COUNT@@": """RULE 1b: The count you state in the introductory sentence MUST equal the TOTAL
number of rows the query returned (row_count / TOTAL ROWS RETURNED), also when the
table only shows 5 of them. Never write "10 buildings" if the query returned 15 rows.""",
        "@@LIST_RULE_6@@": """RULE 6: Up to 10 rows, never truncate a table: no `...`, `…` or "and X more"; every
row from the tool result appears as its own table row. More than 10 rows: show the
first 5 rows and the sentence from RULE 1 (5 of N, full list attached below) — that
is the only allowed shortening.""",
    },
}


def chat_instructions(full_lists: bool = True) -> str:
    """CHAT_INSTRUCTIONS with the list rules of the chosen mode."""
    text = _CHAT_INSTRUCTIONS_TEMPLATE
    for marker, rule in _LIST_RULES[bool(full_lists)].items():
        text = text.replace(marker, rule)
    return text


CHAT_INSTRUCTIONS = chat_instructions(True)

# ── Provider model lists ───────────────────────────────────────────────────────
ANTHROPIC_MODELS = [
    "claude-opus-4-7",
    "claude-sonnet-4-6",
]

OPENAI_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
]

MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", "10"))


# ── Compact-mode auto-routing ──────────────────────────────────────────────────

# Models known to handle full mode well regardless of parameter count.
FULL_MODE_LOCAL_ALLOWLIST: set[str] = {
    "gpt-oss:120b",
    "gpt-oss:20b",
    # add others as validated
}

# Parameter-count threshold (billions) below which we default to compact.
# Models with >= 14 B parameters get the full prompt; 13 B and below get compact.
COMPACT_PARAM_THRESHOLD_B: int = 14

# Context-window threshold below which we always force compact.
COMPACT_CTX_THRESHOLD: int = 16384

_PARAM_RE = re.compile(r"[:\-_](\d+)b\b", re.IGNORECASE)


def _parse_param_count_b(model: str) -> int | None:
    """Extract parameter count in billions from a model name.

    Examples:
      qwen2.5:32b-instruct → 32
      ministral-3:14b      → 14
      gpt-oss:120b         → 120
      claude-sonnet-4-6    → None
    """
    m = _PARAM_RE.search(model.lower())
    return int(m.group(1)) if m else None


def should_use_compact(provider: str, model: str) -> bool:
    """Auto-routing decision for compact vs full prompt mode.

    Returns True when compact mode is recommended for this provider/model.
    The user's radio selection in the UI overrides this when not 'auto'.

    Logic (first match wins):
      1. Cloud providers always use full mode.
      2. Models in FULL_MODE_LOCAL_ALLOWLIST always use full mode.
      3. Native context < COMPACT_CTX_THRESHOLD → compact.
      4. Parameter count < COMPACT_PARAM_THRESHOLD_B → compact.
      5. Default: full mode.
    """
    if provider in ("anthropic", "openai"):
        return False
    if model in FULL_MODE_LOCAL_ALLOWLIST:
        return False

    ctx = _get_provider_ctx_limit(provider, model)
    if ctx > 0 and ctx < COMPACT_CTX_THRESHOLD:
        return True

    params_b = _parse_param_count_b(model)
    if params_b is not None and params_b < COMPACT_PARAM_THRESHOLD_B:
        return True

    return False


# ── Provider discovery ─────────────────────────────────────────────────────────

def get_ollama_models() -> list[str]:
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    try:
        req = urllib.request.Request(f"{base}/api/tags", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def _ollama_root_from_openai_base(base_url: str) -> str:
    """Strip only the OpenAI-compatible Ollama /v1 suffix from a base URL."""
    base = (base_url or "").rstrip("/")
    if base.lower().endswith("/v1"):
        base = base[:-3]
    return base.rstrip("/")


def get_openai_ollama_models() -> list[str]:
    """Return Ollama models exposed through an OpenAI-compatible endpoint."""
    if os.environ.get("OPENAI_API_KEY", "").strip().lower() != "ollama":
        return []
    base = _ollama_root_from_openai_base(
        os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE", "")
    )
    if not base:
        return []
    try:
        req = urllib.request.Request(
            f"{base}/api/tags", headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        return [model["name"] for model in data.get("models", []) if model.get("name")]
    except Exception:
        return []


def openai_uses_ollama() -> bool:
    """Whether OpenAI mode has successfully discovered an Ollama endpoint."""
    return bool(get_openai_ollama_models())



def _ollama_reachable() -> bool:
    base = os.environ.get("OLLAMA_BASE_URL", "").rstrip("/")
    if not base:
        return False
    try:
        with urllib.request.urlopen(
            urllib.request.Request(f"{base}/api/tags"), timeout=3
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def detect_default_provider() -> str | None:
    # Prefer configured providers in this order. The user can still switch to
    # another provider in the UI at any time.
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE"):
        return "openai"
    if _ollama_reachable():
        return "ollama"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def models_for_provider(provider: str) -> list[str]:
    if provider == "anthropic":
        return ANTHROPIC_MODELS
    if provider == "openai":
        return get_openai_ollama_models() or OPENAI_MODELS
    if provider == "ollama":
        return get_ollama_models()
    return []


def default_model(provider: str) -> str:
    models = models_for_provider(provider)
    return models[0] if models else ""


# ── litellm helpers ────────────────────────────────────────────────────────────

def _litellm_model_name(provider: str, model: str) -> str:
    if provider == "ollama":
        return f"ollama/{model}"
    return model


# Models that have told us (via a prior BadRequestError) that they reject the
# `temperature` param outright — e.g. some newer Anthropic models respond with
# "temperature is deprecated for this model". Populated at runtime; not knowable
# ahead of time since Anthropic adds these restrictions per new model release.
_NO_TEMPERATURE_MODELS: set[str] = set()
_NO_REASONING_EFFORT_MODELS: set[tuple[str, str]] = set()


def _litellm_kwargs(
    provider: str,
    model: str,
    temperature: float | None,
    num_ctx: int | None = None,
    enable_thinking: bool | str | None = None,
) -> dict:
    if provider == "ollama":
        default_timeout = os.environ.get("LITELLM_TIMEOUT_LOCAL", "300")
    else:
        default_timeout = os.environ.get("LITELLM_TIMEOUT", "120")
    kw: dict = {
        "model": _litellm_model_name(provider, model),
        "stream": False,
        "timeout": float(default_timeout),
    }
    if temperature is not None:
        kw["temperature"] = temperature
    if provider == "ollama":
        kw["max_tokens"] = int(os.environ.get("LOCAL_MAX_TOKENS", "16000"))
        kw["api_base"] = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        ctx = num_ctx if num_ctx is not None else int(os.environ.get("OLLAMA_NUM_CTX", "131072"))
        kw["extra_body"] = {"options": {"num_ctx": ctx}}
    if provider == "openai":
        openai_base = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")
        if openai_base:
            kw["api_base"] = openai_base
            # litellm infers the provider from known model-name patterns (e.g.
            # "gpt-4o"); a custom local model name like "my-local-model" won't
            # match anything, so it must be told explicitly.
            kw["custom_llm_provider"] = "openai"
        reasoning_effort = {
            False: "none",
            "off": "none",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "max": "max",
            True: "high",
        }.get(enable_thinking)
        reasoning_key = (kw["model"], kw.get("api_base", ""))
        if reasoning_effort and reasoning_key not in _NO_REASONING_EFFORT_MODELS:
            kw["reasoning_effort"] = reasoning_effort
    if kw["model"] in _NO_TEMPERATURE_MODELS:
        kw.pop("temperature", None)
    return kw


def safe_completion(kw: dict, **extra):
    """litellm.completion wrapper that transparently drops `temperature` for
    models that reject it, retrying once and remembering the model so later
    calls in this session skip straight to the no-temperature request."""
    call_kw = {**kw, **extra}
    try:
        return litellm.completion(**call_kw)
    except litellm.BadRequestError as exc:
        if "temperature" in call_kw and "temperature" in str(exc).lower():
            _NO_TEMPERATURE_MODELS.add(call_kw.get("model", ""))
            call_kw.pop("temperature", None)
            return litellm.completion(**call_kw)
        if "reasoning_effort" in call_kw and _is_reasoning_effort_rejection(exc):
            model_key = (call_kw.get("model", ""), call_kw.get("api_base", ""))
            _NO_REASONING_EFFORT_MODELS.add(model_key)
            call_kw.pop("reasoning_effort", None)
            return litellm.completion(**call_kw)
        raise


def _is_reasoning_effort_rejection(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "reasoning_effort" in message
        or "reasoning effort" in message
    ) and any(
        marker in message
        for marker in ("unsupported", "unknown", "invalid", "unrecognized", "not allowed")
    )


# ── Markdown helpers ───────────────────────────────────────────────────────────

def _rows_to_markdown_table(rows: list[dict], max_rows: int = 5) -> str:
    if not rows:
        return "*No rows returned.*"
    headers = list(rows[0].keys())
    header_line = "| " + " | ".join(str(h) for h in headers) + " |"
    sep_line = "| " + " | ".join("---" for _ in headers) + " |"
    body_lines = [
        "| " + " | ".join(str(row.get(h, "")) for h in headers) + " |"
        for row in rows[:max_rows]
    ]
    result = "\n".join([header_line, sep_line] + body_lines)
    if len(rows) > max_rows:
        result += f"\n*… and {len(rows) - max_rows} more rows.*"
    return result


def _is_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except Exception:
        return False


_SQL_KEYWORD_RE = re.compile(
    r"^(SELECT|WITH|INSERT|UPDATE|DELETE)\b", re.IGNORECASE | re.MULTILINE
)

_EMBEDDED_TOOL_CALLS_RE = re.compile(
    r'\n*\s*[Tt]ool[\s_]*[Cc]alls?\s*:\s*[\[\{].*\Z',
    re.DOTALL,
)


def _strip_embedded_tool_calls(text: str) -> str:
    return _EMBEDDED_TOOL_CALLS_RE.sub("", text).rstrip()


_REASONING_TAG_RE = re.compile(
    r"<\s*(think|thinking|thought|thoughts|reasoning)\s*>.*?<\s*/\s*\1\s*>",
    re.DOTALL | re.IGNORECASE,
)
_REASONING_OPEN_RE = re.compile(
    r"<\s*(think|thinking|thought|thoughts|reasoning)\s*>.*\Z",
    re.DOTALL | re.IGNORECASE,
)


def _strip_reasoning_tags(text: str) -> tuple[str, str]:
    """Return (visible_text, reasoning_text) after stripping all reasoning blocks."""
    reasoning_parts: list[str] = []

    def _grab(match: re.Match) -> str:
        reasoning_parts.append(match.group(0))
        return ""

    cleaned = _REASONING_TAG_RE.sub(_grab, text)
    cleaned = _REASONING_OPEN_RE.sub(_grab, cleaned)
    return cleaned.strip(), "\n".join(reasoning_parts).strip()


def _post_process_markdown(text: str) -> str:
    """Strip reasoning tags and embedded tool call JSON, then wrap bare JSON in fences."""
    visible, _reasoning = _strip_reasoning_tags(text)
    visible = _strip_embedded_tool_calls(visible)
    stripped = visible.strip()
    if (stripped.startswith("{") or stripped.startswith("[")) and _is_json(stripped):
        return f"```json\n{stripped}\n```"
    return visible


# ── Tool definition ────────────────────────────────────────────────────────────

RUN_QUERY_TOOL = {
    "type": "function",
    "function": {
        "name": "run_query",
        "description": (
            "Execute a read-only SQL SELECT query against the connected 3DCityDB "
            "database and return the results as JSON."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A read-only SQL SELECT or WITH … SELECT query.",
                }
            },
            "required": ["sql"],
        },
    },
}

_SQL_FENCE_RE = re.compile(r"```sql\n(.*?)```", re.DOTALL | re.IGNORECASE)
MAX_RESULT_CHARS = 20_000


def _truncate_result(raw: str) -> str:
    if len(raw) <= MAX_RESULT_CHARS:
        return raw
    return raw[:MAX_RESULT_CHARS] + f"\n[…truncated — result was {len(raw):,} chars total]"


def _parse_tool_result(raw: str) -> dict:
    """Parse run_query JSON result into structured tool_result event data."""
    try:
        data = json.loads(raw)
        rows = data.get("results", [])
        return {
            "row_count": data.get("row_count", len(rows)),
            "execution_time_ms": data.get("execution_time_ms", 0),
            "preview_rows": rows[:5],
            "all_rows": rows[:200],
            "error": data.get("error") or None,
        }
    except Exception as exc:
        return {
            "row_count": 0,
            "execution_time_ms": 0,
            "preview_rows": [],
            "all_rows": [],
            "error": f"Could not parse tool result: {exc}. Raw: {raw[:200]}",
        }


def _is_all_null_result(result_data: dict) -> bool:
    """Return True when every cell is NULL or every numeric cell is zero.

    A 1-row aggregate result like {"avg_volume_m3": 0.0} is semantically empty
    (e.g. CG_Volume without CG_MakeSolid) and should trigger a retry, not a
    final answer.
    """
    rows = result_data.get("preview_rows", [])
    if not rows:
        return False
    # All cells None
    if all(v is None for row in rows for v in row.values()):
        return True
    # Single-row aggregate where every value is 0 / 0.0
    if len(rows) == 1:
        vals = list(rows[0].values())
        if all(isinstance(v, (int, float)) and v == 0 for v in vals if v is not None):
            return True
    return False


# ── Viz: highlight payload extraction ─────────────────────────────────────────
#
# Payload sent to the 3D viewer (see webmap-overlay/highlight-bridge.js):
#   {"buildings": [{"gmlid": str, "tile_ids": [str]}],   # name kept for the bridge contract
#    "centroid":  {"lat": float, "long": float, "radius_m": float} | None,
#    "missing": [str], "not_tileable": [str]}
# `gmlid` is what the user/LLM asked about; `tile_ids` is what the tileset
# actually contains for it (a Room's boundary surfaces, not the Room itself —
# see citydb_mcp.tools.highlight).

_VIZ_ID_KEYS = ["objectid", "gmlid", "GMLID", "gml_id", "building_id", "feature_id"]
# Any other id-like column, e.g. `room_objectid`, `door_gmlid`.
_VIZ_ID_COL_RE = re.compile(r"(^|_)(objectid|gmlid|gml_id)$", re.IGNORECASE)
# Permissive on purpose: GML ids are not just `BLDG_...` — IFC-derived ones are
# GUIDs or 22-char base64 strings. Tokens that aren't real objectids are dropped
# by the database lookup, so false positives only cost one row in `missing`.
_ID_TOKEN_REGEX = re.compile(r"(?<![\w$-])[\w$-]{8,}(?![\w$-])")
_MAX_HIGHLIGHT_IDS = 200


def _empty_highlight() -> dict:
    return {"buildings": [], "centroid": None, "missing": [], "not_tileable": []}


def _pick_id_key(row: dict) -> str | None:
    key = next((k for k in _VIZ_ID_KEYS if k in row), None)
    if key:
        return key
    return next((k for k in row if _VIZ_ID_COL_RE.search(str(k))), None)


def extract_highlight_payload(events: list) -> dict:
    """Build the viewer payload from the last successful tool result's rows."""
    last_result = None
    for ev_type, ev_data in reversed(events):
        if ev_type == "tool_result" and not ev_data.get("error"):
            last_result = ev_data
            break

    if not last_result or not last_result.get("all_rows"):
        return _empty_highlight()

    rows = last_result["all_rows"]
    found_key = _pick_id_key(rows[0])
    if not found_key:
        return _empty_highlight()

    return build_highlight_payload(
        str(row[found_key]) for row in rows if row.get(found_key)
    )


def fallback_regex_extract(final_answer: str) -> list[str]:
    """Extract candidate GML ids from prose when no tool_result rows are available."""
    candidates = [
        tok for tok in _ID_TOKEN_REGEX.findall(final_answer or "")
        if any(c.isdigit() or c in "_-$" for c in tok)
    ]
    return list(dict.fromkeys(candidates))[:_MAX_HIGHLIGHT_IDS]


_resolver_db = None
_resolver_db_lock = threading.Lock()


def _get_resolver_db():
    """One pooled read-only connection, shared by all sessions (reads CITYDB_* env)."""
    global _resolver_db
    with _resolver_db_lock:
        if _resolver_db is None:
            from citydb_mcp.db import DatabaseConnection
            _resolver_db = DatabaseConnection()
        return _resolver_db


def build_highlight_payload(objectids) -> dict:
    """Resolve objectids to what the viewer can highlight (see the module comment)."""
    ids = list(dict.fromkeys(str(i) for i in objectids if i))[:_MAX_HIGHLIGHT_IDS]
    if not ids:
        return _empty_highlight()

    try:
        from citydb_mcp.tools.highlight import resolve_highlight_targets
        result = resolve_highlight_targets(_get_resolver_db(), ids)
    except Exception as exc:
        # Degrade to highlighting the ids as given (works for features that own
        # geometry themselves); no camera target.
        print(f"[viz] highlight resolution failed: {exc}", flush=True)
        return {
            "buildings": [{"gmlid": g, "tile_ids": [g]} for g in ids],
            "centroid": None, "missing": [], "not_tileable": [],
        }

    return {
        "buildings": [
            {"gmlid": r["objectid"], "tile_ids": r["tile_ids"]}
            for r in result["resolved"]
        ],
        "centroid": result["centroid"],
        "missing": result["missing"],
        "not_tileable": result["not_tileable"],
    }


# ── Viz: flood scene ───────────────────────────────────────────────────────────
#
# A run_query result with a `flood_level_m` column (optionally `protection_m`)
# asks the viewer for a flood scene: a water plane at that height over the
# dataset and every building coloured by whether the water reaches it. The
# chat query only has to carry the level (typically one COUNT row); the
# buildings are fetched here with one fixed query, so neither the 500-row
# run_query cap nor the 200-id highlight cap applies. Rows that also carry
# objectids are the buildings in focus (e.g. "my house").
#
# Payload (sent to webmap-overlay/flood-bridge.js):
#   {"level": float, "protection": float, "from_level": float|None,
#    "gauge": {...}|None, "extent": {west, south, east, north, ...}|None,
#    "buildings": [[objectid, base_m], ...], "extra_ids": {objectid: [tile_id]},
#    "focus": [objectid, ...]}

FLOOD_SCENE_ENABLED = os.environ.get("FLOOD_SCENE", "false").lower() == "true"
# Up to this many focus buildings get the "route" view: positions, camera on the
# first flooded one, lines to the dry ones. Longer lists are only marked.
FLOOD_ROUTE_MAX_FOCUS = 6
_FLOOD_LEVEL_COL = "flood_level_m"
_FLOOD_PROTECTION_COL = "protection_m"
_FLOOD_CACHE_TTL_S = 300
_flood_cache: dict = {}
_flood_cache_lock = threading.Lock()


def _env_list(name: str) -> list[str]:
    return [s.strip() for s in os.environ.get(name, "").split(",") if s.strip()]


def _env_floats(name: str) -> list[float]:
    out = []
    for s in _env_list(name):
        try:
            out.append(float(s))
        except ValueError:
            pass
    return out


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_FLOOD_LEVEL_VARIANTS = ("flood_level", "water_level", "wasserstand")
_FLOOD_CONDITION_RE = re.compile(
    r"ST_ZMin\s*\(\s*(?:\w+\.)?envelope\s*\)\s*<\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)


def _flood_level_column(row: dict) -> str | None:
    """`flood_level_m`, or a close variant local models sometimes write despite
    the prompt (`flood_level_nhn`, `water_level_nhn`, `wasserstand_m`, ...)."""
    if _FLOOD_LEVEL_COL in row:
        return _FLOOD_LEVEL_COL
    return next((k for k in row if str(k).lower().startswith(_FLOOD_LEVEL_VARIANTS)), None)


def extract_flood_scene(events: list) -> dict | None:
    """Level, protection and focus ids from the last successful tool result, if it
    carries a `flood_level_m` column; None otherwise."""
    if not FLOOD_SCENE_ENABLED:
        return None
    last_result, last_sql = None, ""
    for i in range(len(events) - 1, -1, -1):
        ev_type, ev_data = events[i]
        if ev_type == "tool_result" and not ev_data.get("error"):
            last_result = ev_data
            last_sql = next((d.get("args", {}).get("sql", "") for t, d in reversed(events[:i])
                             if t == "tool_call"), "")
            break
    rows = (last_result or {}).get("all_rows") or []
    if not rows:
        return None
    level_col = _flood_level_column(rows[0])
    if level_col is not None:
        level = _as_float(rows[0].get(level_col))
    else:
        # The model left the column out: take the level from the flood
        # condition itself (ST_ZMin(<alias>.envelope) < <level>).
        m = _FLOOD_CONDITION_RE.search(last_sql or "")
        level = _as_float(m.group(1)) if m else None
    if level is None:
        return None
    protection = _as_float(rows[0].get(_FLOOD_PROTECTION_COL)) or 0.0
    id_key = _pick_id_key(rows[0])
    focus = []
    if id_key:
        focus = list(dict.fromkeys(str(r[id_key]) for r in rows if r.get(id_key)))
    return {"level": level, "protection": max(protection, 0.0), "focus": focus[:_MAX_HIGHLIGHT_IDS]}


def _flood_buildings() -> dict:
    """All building base heights (cached for a few minutes; one query per import)."""
    from citydb_mcp.tools.highlight import get_dataset_extent, get_flood_buildings

    exclude = tuple(_env_list("FLOOD_EXCLUDE_FUNCTIONS"))
    now = time.monotonic()
    with _flood_cache_lock:
        hit = _flood_cache.get(exclude)
        if hit and now - hit[0] < _FLOOD_CACHE_TTL_S:
            return hit[1]
    db = _get_resolver_db()
    data = get_flood_buildings(db, exclude)
    data["extent"] = get_dataset_extent(db)
    with _flood_cache_lock:
        _flood_cache[exclude] = (now, data)
    return data


def build_flood_payload(scene: dict) -> dict | None:
    """Expand an extracted flood scene into the viewer payload; None on failure."""
    try:
        data = _flood_buildings()
    except Exception as exc:
        print(f"[viz] flood scene query failed: {exc}", flush=True)
        return None
    gauge = flood_gauge_info()
    from_level = None
    if data["buildings"]:
        from_level = min(b[1] for b in data["buildings"]) - 1.0
    anchor, positions = _focus_route(scene, data)
    return {
        "level": scene["level"],
        "protection": scene["protection"],
        "from_level": from_level,
        "gauge": gauge,
        "extent": data["extent"],
        "buildings": data["buildings"],
        "extra_ids": data["extra_ids"],
        "focus": scene["focus"],
        "anchor": anchor,
        "focus_positions": positions,
    }


def _focus_route(scene: dict, data: dict) -> tuple[str | None, dict]:
    """Anchor (the first flooded focus building, e.g. "my house") and the focus
    buildings' positions, for a short focus list only."""
    focus = scene["focus"]
    if not focus or len(focus) > FLOOD_ROUTE_MAX_FOCUS:
        return None, {}
    bases = {oid: base for oid, base in data["buildings"]}
    anchor = next((o for o in focus if bases.get(o) is not None and bases[o] < scene["level"]), focus[0])
    try:
        from citydb_mcp.tools.highlight import get_focus_positions
        return anchor, get_focus_positions(_get_resolver_db(), focus, anchor)
    except Exception as exc:
        print(f"[viz] focus positions failed: {exc}", flush=True)
        return anchor, {}


# ── Gauge reference (viewer only) ─────────────────────────────────────────────
#
# Optional settings for the viewer's gauge widget: FLOOD_GAUGE_NAME,
# FLOOD_GAUGE_ZERO_M (gauge zero in the data's height system), FLOOD_STAGES
# (warning levels, cm) and FLOOD_REFERENCE_CM / FLOOD_REFERENCE_LABEL (a
# historic flood). They only draw marks next to the water level; they are not
# added to the prompt — tell the chatbot about a gauge in the conversation.

def flood_gauge_info() -> dict | None:
    """Configured gauge reference; None when no gauge zero is set."""
    zero = _as_float(os.environ.get("FLOOD_GAUGE_ZERO_M"))
    if zero is None:
        return None
    return {
        "name": os.environ.get("FLOOD_GAUGE_NAME", "").strip() or None,
        "zero_m": zero,
        "stages_cm": _env_floats("FLOOD_STAGES"),
        "reference_cm": _as_float(os.environ.get("FLOOD_REFERENCE_CM")),
        "reference_label": os.environ.get("FLOOD_REFERENCE_LABEL", "").strip() or None,
    }


FLOOD_SCENE_INSTRUCTIONS = """\
[FLOOD SCENE]
The 3D viewer can show a flood. Add a constant column `flood_level_m` (water level in the \
height system of the data, e.g. `305.0 AS flood_level_m`) to the query that answers a flood \
question, and optionally `protection_m` (height of a property-level flood barrier). Use exactly \
these names (not water_level, not *_nhn): with any other name the viewer shows nothing. The viewer \
then draws the water at that height and colours EVERY building itself (red = flooded, \
green = protected by the barrier).
- A building is flooded ("betroffen", "affected", "unter Wasser") when ST_ZMin(feature.envelope) < \
water level (its lowest point). Always filter on this condition when the user asks which \
buildings are affected, also when searching by name or type; count Buildings (objectclass \
Building) this way so your numbers match the viewer.
- Exception to RULE 1: for "which buildings are flooded" questions do NOT list the buildings. \
Return one row with the count plus `flood_level_m` (and `protection_m`), answer with the count \
and tell the user the 3D view shows every affected building.
- For questions about specific buildings (an address, the nearest dry buildings, the most \
endangered facility) select their `objectid` together with `flood_level_m`, the affected building \
(e.g. the user's house) first. Up to 6 such buildings: the viewer flies to the first flooded one, \
marks it dark red, the dry ones dark green with a dashed line and the straight-line distance to \
each, and fades all other buildings. More than 6: it marks them all in orange.
- Exception to RULE 1 for more than 10 such buildings: keep every row (with `objectid`) in the \
query result so the viewer marks them all, but do NOT print one table row per building. Answer \
with a short summary instead: the number of buildings, grouped by name or function, with the \
greatest water depth of each group, and say that the 3D view marks all of them."""


# SQL examples appended to the [FLOOD SCENE] block. {L} = example water level,
# {X}/{Xd} = the non-building exclusion for alias f/d (empty without FLOOD_EXCLUDE_FUNCTIONS).
# Every example is a plain run_query: the flood column makes the viewer draw it.
_FLOOD_EXAMPLES = """\
Flood examples (water level 305.0 is a placeholder: always use the user's level; keep `flood_level_m`):
1. Number of flooded buildings:
SELECT count(*) AS flooded, {L} AS flood_level_m
FROM feature f
WHERE f.objectclass_id = 901 AND ST_ZMin(f.envelope) < {L}{X};
2. Flooded buildings of given types (codes from the schema's function codelist) or names, with water depth:
SELECT f.objectid, fn.val_string AS function, n.val_string AS name,
       round(({L} - ST_ZMin(f.envelope))::numeric, 2) AS water_depth_m, {L} AS flood_level_m
FROM feature f
JOIN property fn ON fn.feature_id = f.id AND fn.name = 'function'
LEFT JOIN property n ON n.feature_id = f.id AND n.name = 'name'
WHERE f.objectclass_id = 901 AND ST_ZMin(f.envelope) < {L}
  AND (fn.val_string IN ('<code1>', '<code2>') OR n.val_string ILIKE '%<facility name>%'){X}
ORDER BY water_depth_m DESC;
3. Facade area under water (m², 3D area of each wall times its flooded share):
SELECT round(sum(ST_3DArea(g.geometry) * LEAST(GREATEST(({L} - ST_ZMin(g.geometry))
         / NULLIF(ST_ZMax(g.geometry) - ST_ZMin(g.geometry), 0), 0), 1))::numeric) AS facade_under_water_m2,
       {L} AS flood_level_m
FROM feature w JOIN geometry_data g ON g.feature_id = w.id
WHERE w.objectclass_id = 709 AND ST_ZMin(g.geometry) < {L};
4. Effect of a property-level barrier of 1.0 m:
SELECT count(*) FILTER (WHERE ST_ZMin(f.envelope) < {L}) AS affected,
       count(*) FILTER (WHERE ST_ZMin(f.envelope) < {L} AND ST_ZMin(f.envelope) + 1.0 >= {L}) AS protected,
       count(*) FILTER (WHERE ST_ZMin(f.envelope) + 1.0 < {L}) AS still_flooded,
       {L} AS flood_level_m, 1.0 AS protection_m
FROM feature f
WHERE f.objectclass_id = 901{X};
5. Building at an address and the nearest dry building (match the address as the schema describes):
WITH me AS (
  SELECT f.objectid, f.envelope FROM feature f
  JOIN property p ON p.feature_id = f.id AND p.name = 'address'
  JOIN address a ON a.id = p.val_address_id
  WHERE a.street = '<street and house number>')
SELECT me.objectid, 'address' AS role, round(({L} - ST_ZMin(me.envelope))::numeric, 2) AS water_depth_m,
       NULL::numeric AS distance_m, NULL::numeric AS azimuth_deg, {L} AS flood_level_m
FROM me
UNION ALL
SELECT * FROM (
  SELECT d.objectid, 'nearest dry building', NULL::numeric,
         round(ST_Distance(d.envelope, me.envelope)::numeric),
         round(degrees(ST_Azimuth(ST_Centroid(me.envelope), ST_Centroid(d.envelope)))::numeric), {L}
  FROM feature d CROSS JOIN me
  WHERE d.objectclass_id = 901 AND ST_ZMin(d.envelope) > {L}{Xd}
  ORDER BY d.envelope <-> me.envelope LIMIT 1) dry;
(azimuth: 0 = north, 90 = east, 180 = south, 270 = west; LIMIT 3 for the three nearest dry buildings)
6. Flooded buildings per water level (draw as a mermaid xychart-beta line chart):
SELECT l AS water_level_m, count(*) FILTER (WHERE ST_ZMin(f.envelope) < l) AS flooded
FROM feature f, generate_series(300.0, 306.0, 0.5) AS l
WHERE f.objectclass_id = 901{X}
GROUP BY l ORDER BY l;"""


def _flood_exclusion_sql(alias: str = "f") -> str:
    codes = _env_list("FLOOD_EXCLUDE_FUNCTIONS")
    if not codes:
        return ""
    quoted = ", ".join("'" + c.replace("'", "''") + "'" for c in codes)
    return (f"\n  AND NOT EXISTS (SELECT 1 FROM property x WHERE x.feature_id = {alias}.id"
            f" AND x.name = 'function' AND x.val_string IN ({quoted}))")


def flood_scene_examples(level: str = "305.0") -> str:
    """The SQL examples of the [FLOOD SCENE] block for an example water level."""
    return (_FLOOD_EXAMPLES.replace("{L}", level)
            .replace("{Xd}", _flood_exclusion_sql("d"))
            .replace("{X}", _flood_exclusion_sql("f")))


def flood_scene_instructions() -> str:
    """The [FLOOD SCENE] prompt block: convention, exclusions and SQL examples."""
    text = FLOOD_SCENE_INSTRUCTIONS
    exclude = _env_list("FLOOD_EXCLUDE_FUNCTIONS")
    if exclude:
        codes = ", ".join(f"'{c}'" for c in exclude)
        text += ("\n- Not buildings (bridges, walls, roofs without walls, ...): always exclude features "
                 f"whose `function` is one of {codes}, as in the examples; the viewer excludes them too.")
    return text + "\n\n" + flood_scene_examples()


def extra_prompt_blocks() -> str:
    """Prompt additions of optional features (flood scene, scratchpad).

    Appended after the assembled system prompt by every chat entry point
    (web UI and benchmark CLI) so both see the same prompt. Empty by default.
    Dataset knowledge (a gauge, local codes, recipes) belongs in the
    conversation, not here.
    """
    blocks = []
    if FLOOD_SCENE_ENABLED:
        blocks.append(flood_scene_instructions())
    if scratchpad_enabled():
        blocks.append(SCRATCHPAD_TOOLS_INSTRUCTIONS)
    return "\n\n".join(blocks)


# ── Spatial scratchpad (per-user saved results) ───────────────────────────────
# The model saves, changes and deletes items with the MCP tools
# save_to_scratchpad / update_scratchpad_item / scratchpad_action /
# list_scratchpad (see production/docs/scratchpad.md), and reuses them in
# run_query through the view my_scratchpad.

def scratchpad_enabled() -> bool:
    from citydb_mcp.tools import scratchpad

    return scratchpad.enabled()


def scratchpad_tool_specs() -> list[dict]:
    """The scratchpad tools in OpenAI function format ([] unless the scratchpad is on)."""
    if not scratchpad_enabled():
        return []
    from citydb_mcp.tools import scratchpad

    return [{"type": "function",
             "function": {"name": n, "description": spec["description"],
                          "parameters": spec["inputSchema"]}}
            for n, spec in scratchpad.TOOL_SCHEMAS.items()]


SCRATCHPAD_TOOLS_INSTRUCTIONS = """[SCRATCHPAD] The user's personal saved items, shown in the 3D viewer
- Tools: save_to_scratchpad (save a result under a name), update_scratchpad_item (change label,
  color, opacity, class or size of a saved item in place), scratchpad_action (show, hide, delete,
  goto), list_scratchpad (what is saved). Use them whenever the user asks to save, store, mark,
  pin, remember, recolour, show/hide or delete something ("speichern", "merken", "markieren",
  "umfärben", ...).
- Deleting SEVERAL items: first ask the user to confirm the list; call scratchpad_action with
  confirm=true only after a yes. Deleting one item needs no confirmation.
- Names: letters, digits and underscore, no spaces (e.g. affected_2013). Pick a short name
  yourself if the user gives none, and tell it.
- save_to_scratchpad with `sql`: a SELECT returning objectid rows (a set of features, coloured in
  the viewer) OR a geom column (a geometry in the data's CRS; several rows become one item).
  With `geojson`: a GeoJSON geometry/Feature/FeatureCollection in lon/lat (e.g. points the user
  pasted); it is converted to the data's CRS. Optional: label, color ('#rrggbb'), clamp,
  class (how to draw a geometry; derived otherwise: 2Dmarker/3Dmarker pin, Point3D sphere,
  LineString3D pipe, Polygon2D/3D, Solid3D, ...), size_m (sphere/pipe radius).
  The tool runs the SQL itself and reports the saved count; report name and count.
- 2D/3D: the class and the stored geometry always match ("POLYGON Z" = with heights). To give a
  saved item a height ("add height", "make it 3D", "20 m hoch") call update_scratchpad_item with
  height_m (metres above ground: 2Dmarker -> 3Dmarker, Polygon2D -> Polygon3D); to turn an area
  into a solid ("Körper", "solid", "extrudieren") use extrude_m (Polygon2D -> Solid3D).
  base_z = absolute height instead of the ground. Changing only the class does not add a height.
- Bounding boxes: 3D box `ST_3DExtent(f.envelope) AS geom` (saved as a closed box, Solid3D);
  2D `ST_Extent(f.envelope) AS geom`. ST_MakeEnvelope has no heights.
- REUSE saved items in run_query through the view my_scratchpad(name, label, kind, class, color, geom,
  objectids text[], is_visible, data, created_at). There is no other scratchpad table. Select
  cardinality(objectids) and GeometryType(geom) rather than the raw columns when listing.

Scratchpad examples:
save_to_scratchpad {"name": "tall_buildings", "color": "#dc2626", "sql":
  "SELECT f.objectid FROM feature f WHERE f.objectclass_id = 901 AND ST_ZMax(f.envelope) - ST_ZMin(f.envelope) > 30"}
save_to_scratchpad {"name": "meeting_point", "label": "Meeting point",
  "geojson": {"type": "Point", "coordinates": [13.4735, 48.5740]}}
save_to_scratchpad {"name": "cluster_hull", "sql":
  "SELECT CG_3DConvexHull(ST_Collect(f.envelope)) AS geom FROM feature f WHERE f.objectid IN ('<id1>', '<id2>')"}
update_scratchpad_item {"name": "meeting_point", "height_m": 20}
save_to_scratchpad {"name": "set_box", "sql": "SELECT ST_3DExtent(f.envelope) AS geom FROM feature f
  JOIN my_scratchpad p ON f.objectid = ANY(p.objectids) WHERE p.name = 'tall_buildings'"}
run_query: buildings within 100 m of a saved point
  SELECT count(*) FROM feature f, my_scratchpad p WHERE p.name = 'meeting_point'
    AND f.objectclass_id = 901 AND ST_DWithin(f.envelope, p.geom, 100)"""


def note_scratch_result(raw_result: str, focus: list) -> bool:
    """Read the "scratchpad" entry of a scratchpad tool result. Collects names the
    viewer should fly to (new items, goto) and returns True if anything changed."""
    try:
        report = json.loads(raw_result).get("scratchpad")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return False
    if not report:
        return False
    changed = False
    for r in report if isinstance(report, list) else [report]:
        if not isinstance(r, dict) or not r.get("ok"):
            continue
        changed = True
        if r.get("kind") in ("geometry", "featureset"):  # a new item: show it
            focus.append(r["name"])
        elif r.get("action") == "goto":
            focus.extend(r.get("found") or [])
    return changed


def build_scratchpad_payload(user_id: str, focus=()) -> dict | None:
    """The user's items for the viewer (scratchpad-bridge.js); None on error."""
    if not scratchpad_enabled():
        return None
    from citydb_mcp.tools import scratchpad

    try:
        items = scratchpad.list_items(_get_resolver_db(), user_id)
    except Exception as exc:
        print(f"[scratchpad] could not list items: {exc}", flush=True)
        return None
    return {"items": items, "focus": [n for n in dict.fromkeys(focus or [])]}


# Questions after which the model may still call a scratchpad tool once it has
# a query result (the cloud backend otherwise forces a text answer then).
_SCRATCH_INTENT_RE = re.compile(
    r"speicher|merk|markier|notier|lösch|losch|ausblend|einblend|blende|gespeichert|"
    r"\bsave|\bstore|remember|\bmark|\bpin\b|delete|\bhide|\bshow\b.*\b(saved|my)\b|scratchpad",
    re.IGNORECASE)


def wants_scratchpad(question: str) -> bool:
    return bool(_SCRATCH_INTENT_RE.search(question or ""))


def scratch_tool_result(raw: str) -> dict:
    """tool_result event data for a scratchpad tool: the report as preview rows
    (shown in Agent activity), no all_rows (nothing to highlight), the error
    if the tool reported one."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        data = {"error": str(raw)[:300]}
    if not isinstance(data, dict):
        data = {"result": data}
    if "items" in data:
        rows = data["items"] if isinstance(data["items"], list) else []
    else:
        report = data.get("scratchpad", data)
        rows = report if isinstance(report, list) else [report]
    rows = [r for r in rows if isinstance(r, dict)]
    errors = [str(r["error"]) for r in rows if r.get("ok") is False and r.get("error")]
    if data.get("error"):
        errors.append(str(data["error"]))
    return {"row_count": len(rows), "execution_time_ms": 0, "preview_rows": rows[:10],
            "all_rows": [], "error": "; ".join(errors) or None,
            "needs_confirmation": any(r.get("needs_confirmation") for r in rows)}


def scratch_tool_hint(result_data: dict) -> str:
    """What the model is told after a scratchpad tool call."""
    if result_data.get("needs_confirmation"):
        return ("Do NOT call the tool again now. Ask the user to confirm deleting these items and "
                "stop; only after the user says yes, call it again with confirm=true.")
    if result_data.get("error"):
        return f"The tool reported an error: {result_data['error']}. Fix the arguments and call it again."
    return "Tell the user briefly what was done (name and count)."



# ── Full result attached under long answers ───────────────────────────────────
# Above FULL_RESULT_MIN_ROWS rows the app attaches the complete result of the
# last query as collapsible CSV and JSON, ready to copy (in both list modes,
# see _LIST_RULES). It stays in the chat, but strip_full_result() removes it
# before the history goes back to the model.

FULL_RESULT_MIN_ROWS = 10
_FULL_RESULT_RE = re.compile(r"\s*<details data-full-result>.*?</details>", re.DOTALL)


def _long_result_rows(raw_result: str) -> list[dict]:
    """The rows of a run_query result with more than FULL_RESULT_MIN_ROWS rows; [] otherwise."""
    try:
        data = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError):
        return []
    rows = data.get("results") if isinstance(data, dict) and data.get("success") else None
    if not rows or len(rows) <= FULL_RESULT_MIN_ROWS or not isinstance(rows[0], dict):
        return []
    return rows


def _rows_to_csv(rows: list[dict]) -> str:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().rstrip()


def full_result_csv(raw_result: str) -> str:
    """Plain-text full list for the terminal (CLI short-list mode); "" if not long."""
    rows = _long_result_rows(raw_result)
    if not rows:
        return ""
    return f"\n\nFull list ({len(rows)} rows, CSV):\n{_rows_to_csv(rows)}"


def format_full_result(raw_result: str) -> str:
    """Collapsible CSV + JSON of a run_query result with more than
    FULL_RESULT_MIN_ROWS rows; "" otherwise."""
    rows = _long_result_rows(raw_result)
    if not rows:
        return ""
    capped = " (first rows only: the query hit the row limit)" if len(rows) >= 500 else ""
    js = json.dumps(rows, ensure_ascii=False, default=str, indent=1)
    out = []
    for fmt, body in (("CSV", _rows_to_csv(rows)), ("JSON", js)):
        out.append(f"<details data-full-result>\n<summary>📋 Full list as {fmt} ({len(rows)} rows){capped}"
                   f"</summary>\n\n```{fmt.lower()}\n{body}\n```\n\n</details>")
    return "\n\n" + "\n\n".join(out)


def long_result_note(count: int, full_lists: bool = True) -> str:
    """Reminder placed right in a long tool result (RULE 1, short lists only):
    local models follow the freshest text more than the system prompt."""
    if full_lists or (count or 0) <= FULL_RESULT_MIN_ROWS:
        return ""
    return (f"\nLONG RESULT ({count} rows): in your answer show ONLY the first 5 rows in the table, "
            f"then one sentence that these are 5 of {count} and the full list is attached below "
            "the answer (CSV/JSON). Do not write the other rows and do not query again for them.")


def strip_full_result(text: str) -> str:
    return _FULL_RESULT_RE.sub("", text or "")
