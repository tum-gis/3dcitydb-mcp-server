"""Shared LLM provider utilities: stop flag, context estimation, model lists, helpers."""

import concurrent.futures as _cf
import json
import os
import re
import threading
import urllib.request

import litellm

_executor = _cf.ThreadPoolExecutor(max_workers=8)

# ── Stop flag ──────────────────────────────────────────────────────────────────
_stop_event = threading.Event()


def request_stop() -> None:
    print("[llm] request_stop() called — setting stop flag + cancelling active future", flush=True)
    _stop_event.set()
    _cancel_active_future()


def _clear_stop() -> None:
    _stop_event.clear()


def _is_stopped() -> bool:
    return _stop_event.is_set()


_active_future: "_cf.Future | None" = None
_active_future_lock = threading.Lock()


def _set_active_future(fut: "_cf.Future | None") -> None:
    global _active_future
    with _active_future_lock:
        _active_future = fut


def _cancel_active_future() -> None:
    with _active_future_lock:
        fut = _active_future
    if fut is not None:
        fut.cancel()


# ── Context-usage estimation & logging ────────────────────────────────────────

_ollama_ctx_cache: dict[str, int] = {}


def _estimate_tokens(messages: list[dict]) -> int:
    """Rough token count using 3 chars ≈ 1 token heuristic."""
    return sum(len(str(m.get("content") or "")) for m in messages) // 3


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
    configured = int(os.environ.get("OLLAMA_NUM_CTX", "32768"))
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
CHAT_INSTRUCTIONS = """\
You are a SQL query assistant for 3DCityDB v5 — a PostgreSQL-based 3D city model database \
following the CityGML standard. The user is exploring a city dataset and wants \
natural-language answers backed by real data.

**Tool-use discipline:**
- If the user sends a greeting, thank-you, or other non-query message (e.g. "hello", \
"thanks", "what can you do?"), reply conversationally with a Final Answer — do NOT call \
`run_query`. Only call `run_query` when the user is asking about data in the database.
- You have direct database access via `run_query`. For any data question, ALWAYS call it — \
never write SQL in your chat reply expecting the user to run it.
- The user sees the SQL in the Agent Activity panel. Only paste SQL text in your chat reply, if the user explicitly asks for it.
- When the user asks "show me", "list", "which", or any counting question: \
always write a query that SELECTs `feature.objectid AS objectid` plus relevant name/type columns — \
NEVER use `SELECT COUNT(*) alone` unless the user explicitly asks for a count. \
Your final answer must always include the objectid of each matching feature; \
the UI uses these to highlight objects on a map.
- Property names live in a specific namespace_id — always use the namespace_id given in the schema for each property (schema properties are typically ns:8 or ns:10; only generic attributes are ns:3) — and when the question asks about a grouping entity (street, owner, usage), GROUP BY that entity alone, never by feature.objectid.
- Some properties (e.g. height) are nested containers whose own val_* columns are NULL — if the schema marks a property as ⚠️ NESTED TYPE, you MUST join property→property via parent_id to a child row where name = 'value' and read val_double from the child, never from the parent.

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
from the result. The table MUST contain EVERY row returned by the query — never
omit, filter out, or truncate rows, even if they have NULL values in some columns
(show NULL as an empty cell). Do NOT narrate rows as prose. Do NOT group rows by
shared values and describe them sentence-by-sentence.

RULE 1b: The count you state in the introductory sentence MUST equal the number
of rows in your table, which MUST equal the number of rows the query returned.
Never write "10 buildings" if the query returned 15 rows.

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

RULE 6: Never truncate a table. Never use `...` or `…` or "and X more" to shorten
the table. Every row from the tool result MUST appear as its own table row.
If this makes the answer long — that is correct and expected.

Examples of CORRECT formatting:

User: "Show me all residential buildings in Röblingweg."
Tool result: 4 rows with objectid and function
Answer:
The following **4** residential buildings are located in Röblingweg:

| objectid           | function     |
|--------------------|--------------|
| DEBY_LOD2_4965683  | residential  |
| DEBY_LOD2_4965796  | residential  |
| DEBY_LOD2_4965797  | residential  |
| DEBY_LOD2_4965798  | residential  |

**Error handling:**
If a query fails, fix it silently and retry. Do not ask the user for help unless you've \
tried 3 different approaches.

**Honesty:**
If you genuinely cannot answer with the available data, say so directly. Do not invent \
objectclass IDs, property names, or values — use only what's in the assembled schema below.
"""

# ── Provider model lists ───────────────────────────────────────────────────────
ANTHROPIC_MODELS = [
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
]

OPENAI_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
]

MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", "6"))


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
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE"):
        return "openai"
    if _ollama_reachable():
        return "ollama"
    return None


def models_for_provider(provider: str) -> list[str]:
    if provider == "anthropic":
        return ANTHROPIC_MODELS
    if provider == "openai":
        return OPENAI_MODELS
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


def _litellm_kwargs(
    provider: str,
    model: str,
    temperature: float,
    num_ctx: int | None = None,
) -> dict:
    if provider == "ollama":
        default_timeout = os.environ.get("LITELLM_TIMEOUT_LOCAL", "300")
    else:
        default_timeout = os.environ.get("LITELLM_TIMEOUT", "120")
    kw: dict = {
        "model": _litellm_model_name(provider, model),
        "temperature": temperature,
        "stream": False,
        "timeout": float(default_timeout),
    }
    if provider == "ollama":
        kw["max_tokens"] = int(os.environ.get("LOCAL_MAX_TOKENS", "16000"))
        kw["api_base"] = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        ctx = num_ctx if num_ctx is not None else int(os.environ.get("OLLAMA_NUM_CTX", "32768"))
        kw["extra_body"] = {"options": {"num_ctx": ctx}}
    if provider == "openai":
        openai_base = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")
        if openai_base:
            kw["api_base"] = openai_base
            # litellm infers the provider from known model-name patterns (e.g.
            # "gpt-4o"); a custom local model name like "my-local-model" won't
            # match anything, so it must be told explicitly.
            kw["custom_llm_provider"] = "openai"
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
        raise


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

_VIZ_ID_KEYS = ["objectid", "gmlid", "GMLID", "gml_id", "building_id", "feature_id"]
_GMLID_REGEX = re.compile(r'\b([A-Z]{2,5}_[A-Z0-9_-]{5,})\b')


def extract_highlight_payload(events: list) -> dict:
    """Build {buildings: [{gmlid}], centroid: [{lat, long}]|None} from event stream."""
    last_result = None
    for ev_type, ev_data in reversed(events):
        if ev_type == "tool_result" and not ev_data.get("error"):
            last_result = ev_data
            break

    if not last_result or not last_result.get("all_rows"):
        return {"buildings": [], "centroid": None}

    rows = last_result["all_rows"]
    found_key = next((k for k in _VIZ_ID_KEYS if k in rows[0]), None)
    if not found_key:
        return {"buildings": [], "centroid": None}

    gmlids = [str(row[found_key]) for row in rows if row.get(found_key)]
    gmlids = list(dict.fromkeys(gmlids))[:200]
    if not gmlids:
        return {"buildings": [], "centroid": None}

    centroid = _compute_centroid_wgs84(gmlids)
    return {
        "buildings": [{"gmlid": gid} for gid in gmlids],
        "centroid": [{"lat": centroid[0], "long": centroid[1]}] if centroid else None,
    }


def fallback_regex_extract(final_answer: str) -> list[str]:
    """Regex fallback: extract GMLIDs from prose when no tool_result rows are available."""
    return list(dict.fromkeys(_GMLID_REGEX.findall(final_answer)))[:200]


def _compute_centroid_wgs84(gmlids: list[str]) -> tuple[float, float] | None:
    """Query PostGIS for the WGS84 centroid of the union of envelopes for these features."""
    if not gmlids:
        return None
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        conn_params = {
            "host": os.environ.get("CITYDB_HOST", "localhost"),
            "port": int(os.environ.get("CITYDB_PORT", "5432")),
            "dbname": os.environ.get("CITYDB_NAME", "citydb"),
            "user": os.environ.get("CITYDB_USER", "citydb"),
            "password": os.environ.get("CITYDB_PASSWORD", "citydb"),
        }
        schema = os.environ.get("CITYDB_SCHEMA", "citydb")
        placeholders = ",".join(["%s"] * len(gmlids))
        sql = f"""
            SELECT
              ST_Y(ST_Centroid(ST_Transform(ST_Union(envelope), 4326))) AS lat,
              ST_X(ST_Centroid(ST_Transform(ST_Union(envelope), 4326))) AS lng
            FROM {schema}.feature
            WHERE objectid IN ({placeholders})
              AND envelope IS NOT NULL;
        """
        with psycopg2.connect(**conn_params) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, gmlids)
                row = cur.fetchone()
                if row and row["lat"] is not None:
                    return (float(row["lat"]), float(row["lng"]))
    except Exception as exc:
        print(f"[viz] centroid computation failed: {exc}", flush=True)
    return None
