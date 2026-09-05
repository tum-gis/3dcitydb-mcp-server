"""LangChain ReAct backend for Ollama.

Uses ChatOllama + create_react_agent + AgentExecutor.
The MCP-assembled system prompt replaces the hardcoded schema from the old demo.
A callback handler converts LangChain agent events to the shared event protocol.
"""

import json
import os
import queue
import re
import threading
import time as _time
from typing import Callable, Generator

from langchain.agents import AgentExecutor, create_react_agent
from langchain.tools import Tool
from langchain_core.agents import AgentAction, AgentFinish
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.agents.output_parsers import ReActSingleInputOutputParser

from webui.llm_utils import (
    MAX_ITERATIONS,
    _is_stopped,
    _log_context_usage,
    _parse_tool_result,
    _post_process_markdown,
    _strip_reasoning_tags,
)

# ── SQL input cleaning (lifted verbatim from old_ollama_demo.py) ───────────────
_SQL_KW_RE = re.compile(r"^(SELECT|WITH|INSERT|UPDATE|DELETE)\b", re.IGNORECASE | re.MULTILINE)

# ── Static test prompt (swap with MCP-assembled prompt to isolate prompt quality) ──
_STATIC_PROMPT = """\
# 3DCityDB v5 SQL Assistant

You query a PostgreSQL database of CityGML 3D building data via the run_query tool.
Always call run_query — never write SQL in your chat reply.

## Object types (feature.objectclass_id)
- 901 = Building
- 709 = WallSurface
- 710 = GroundSurface
- 712 = RoofSurface

## Building function codes (property.name = 'function', val_string)
- 31001_1000 = Wohngebäude (residential)
- 31001_2000 = Gebäude für Wirtschaft oder Gewerbe
- 31001_3000 = Gebäude für öffentliche Zwecke
- 31001_3041 = Kirche

## Key tables (only columns you need)

feature: id, objectclass_id, objectid, envelope
property: id, feature_id, parent_id, name, val_string, val_int, val_double, val_timestamp, val_address_id, val_feature_id, val_relation_type
address: id, street, house_number, zip_code, city
geometry_data: id, feature_id, geometry, geometry_properties

## JOIN recipes

Feature properties:
  JOIN property p ON p.feature_id = f.id
  WHERE p.name = '<attribute>' AND p.val_string = '<value>'

Addresses:
  JOIN property p ON p.feature_id = f.id AND p.name = 'address'
  JOIN address a ON a.id = p.val_address_id

Height (nested):
  JOIN property parent ON parent.feature_id = f.id AND parent.name = 'height'
  JOIN property child ON child.parent_id = parent.id AND child.name = 'value'
  -- height value is in child.val_double

Boundary surfaces (roof, wall, ground):
  JOIN property rel ON rel.feature_id = f.id AND rel.val_relation_type = 1
  JOIN feature s ON s.id = rel.val_feature_id AND s.objectclass_id IN (709, 710, 712)

Geometry (volume vs surface area):
  JOIN geometry_data g ON g.feature_id = f.id
  -- Volume: WHERE (g.geometry_properties->>'type')::int IN (8, 9)
  -- Surface area: WHERE (g.geometry_properties->>'type')::int IN (3, 4)

## SFCGAL functions
- CG_Volume(CG_MakeSolid(geometry)) — volume in m³ (geometry must be closed)
- CG_3DArea(geometry) — true 3D surface area

## Example queries

Residential buildings on a specific street:
  SELECT f.objectid
  FROM feature f
  JOIN property p_func ON p_func.feature_id = f.id AND p_func.name = 'function'
  JOIN property p_addr ON p_addr.feature_id = f.id AND p_addr.name = 'address'
  JOIN address a ON a.id = p_addr.val_address_id
  WHERE f.objectclass_id = 901
    AND p_func.val_string = '31001_1000'
    AND a.street ILIKE '%Röblingweg%';

Tallest buildings:
  SELECT f.objectid, child.val_double AS height_m
  FROM feature f
  JOIN property parent ON parent.feature_id = f.id AND parent.name = 'height'
  JOIN property child ON child.parent_id = parent.id AND child.name = 'value'
  WHERE f.objectclass_id = 901
  ORDER BY child.val_double DESC LIMIT 5;

Oldest buildings:
  SELECT f.objectid, p.val_timestamp AS construction_date
  FROM feature f
  JOIN property p ON p.feature_id = f.id AND p.name = 'dateOfConstruction'
  WHERE f.objectclass_id = 901
  ORDER BY p.val_timestamp ASC LIMIT 5;

Building volume:
  SELECT f.objectid, CG_Volume(CG_MakeSolid(g.geometry)) AS volume_m3
  FROM feature f
  JOIN geometry_data g ON g.feature_id = f.id
  WHERE f.objectclass_id = 901
    AND ST_IsClosed(g.geometry) = true
    AND (g.geometry_properties->>'type')::int IN (8, 9)
  ORDER BY volume_m3 DESC LIMIT 10;

Total roof area on a street:
  SELECT a.street, SUM(CG_3DArea(g.geometry)) AS total_roof_m2
  FROM feature b
  JOIN property p_addr ON p_addr.feature_id = b.id AND p_addr.name = 'address'
  JOIN address a ON a.id = p_addr.val_address_id
  JOIN property rel ON rel.feature_id = b.id AND rel.val_relation_type = 1
  JOIN feature s ON s.id = rel.val_feature_id AND s.objectclass_id = 712
  JOIN geometry_data g ON g.feature_id = s.id
  WHERE b.objectclass_id = 901 AND a.street ILIKE '%Röblingweg%'
  GROUP BY a.street;

## Rules
- Always include f.objectid in SELECT for "show/list/which" questions.
- Always filter by objectclass_id to avoid full table scans.
- Use ILIKE '%street%' for street name matches (handles umlauts, partial names).
- Return only the answer in the user's language. No SQL in the chat reply.
"""

# Set to True to use the static prompt above instead of the MCP-assembled one.
_USE_STATIC_PROMPT = False


def _escape_template_braces(text: str) -> str:
    """Double every literal brace in ``text`` so that a
    ``ChatPromptTemplate`` does not interpret them as template variables.

    The ReAct prompt embeds the (dynamic) MCP system content verbatim into a
    ``ChatPromptTemplate``. Any stray ``{...}`` in that content — e.g. a JSON
    example such as ``{"sql": "..."}`` — would otherwise be parsed as a
    missing template variable and break agent construction. The format block
    that is appended separately already escapes its own braces, so only the
    dynamic content needs doubling here.
    """
    return text.replace("{", "{{").replace("}", "}}")


def _clean_sql_input(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^\s*json\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"```[a-zA-Z]*\n?", "", raw)
    raw = raw.replace("```", "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            sql = data.get("sql") or data.get("query") or data.get("input_data", "")
            if sql:
                return str(sql).strip()
        except json.JSONDecodeError:
            for key in ("sql", "query", "input_data"):
                m = re.search(
                    r'"' + key + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL
                )
                if m:
                    return m.group(1).strip()
    if _SQL_KW_RE.match(raw.lstrip()):
        return raw.strip()
    return raw.strip("{}").strip()


# ── LangChain callback → event queue ──────────────────────────────────────────

_SENTINEL = object()  # signals agent thread finished


class _EventCallback(BaseCallbackHandler):
    """Puts agent events onto a queue consumed by react_stream."""

    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self._q = q
        self._iteration = 0
        self._tools_called = 0
        self._parse_errors = 0  # counts _require_tool_call failures to break the retry loop
        # Consecutive unparseable LLM responses WITHOUT a successful tool
        # execution in between (see _RobustReActParser._record_parse_failure).
        # A model that keeps emitting an unparseable action (e.g. granite4.2's
        # `Action: {"sql": ...}` without the `Action Input:` line) would
        # otherwise loop for hours until the scratchpad bloats past the context
        # limit. Reset in on_tool_start so the budget is per-format-loop.
        self._consecutive_parse_failures = 0
        self._stream_buffer = []   # accumulates tokens for the current LLM call
        self._streaming_final = False  # True once "Final Answer:" seen in stream
        self._in_error_retry = False  # True while LangChain replays a parse-error correction
        self._raw_buffer = ""      # batches raw tokens for live thinking_token events
        self._last_flush = _time.monotonic()
        # Repetition guard (see _RobustReActParser._check_repetition): track the
        # last executed (tool, sql) key and how many times it has run in a row.
        # A model that re-issues the exact same query forever (observed with
        # granite4.2 at temp 0.1) is degenerate -- every response parses and
        # executes, so the parse-loop breaker never trips. Capped by
        # max_iterations, but 10x identical queries waste minutes of inference.
        self._last_action_key = None
        self._consecutive_repeats = 0
        self._llm_calls = 0

    @staticmethod
    def _parse_tool_output(output: str) -> dict:
        """Parse the human-readable string returned by _run_query into a structured dict."""
        s = output.strip()
        if s.startswith("Error:"):
            return {"row_count": 0, "execution_time_ms": 0, "preview_rows": [], "all_rows": [], "error": s}
        if s.startswith("No results found"):
            return {"row_count": 0, "execution_time_ms": 0, "preview_rows": [], "all_rows": [], "error": None}
        if "The result is:" in s:
            json_part = s.split("The result is:", 1)[1].strip()
            rows = []
            for line in json_part.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return {
                "row_count": len(rows),
                "execution_time_ms": 0,
                "preview_rows": rows[:5],
                "all_rows": rows[:200],
                "error": None,
            }
        return {"row_count": 0, "execution_time_ms": 0, "preview_rows": [], "all_rows": [], "error": None}

    def on_agent_action(self, action, **kwargs):
        # LangChain fires on_agent_action for _Exception (parse-error correction)
        # actions too — suppress them so the error never leaks into the UI.
        if getattr(action, "tool", "") == "_Exception":
            return
        # The Thought text was already streamed live token-by-token via
        # on_llm_new_token / thinking_token — nothing to emit here anymore.

    def _flush_raw(self) -> None:
        """Push whatever raw text has accumulated since the last flush."""
        if self._raw_buffer:
            self._q.put(("thinking_token", self._raw_buffer))
            self._raw_buffer = ""

    def on_tool_start(self, serialized, input_str, **kwargs):
        # Flush any trailing Thought text before the tool call is announced,
        # so the live reasoning stream finishes before "Calling run_query" shows.
        self._flush_raw()
        # LangChain routes parse-error corrections through a synthetic "_Exception"
        # tool. Suppress these — they are not real DB queries and must not appear
        # in the UI or increment the tools-called counter.
        tool_name = serialized.get("name", "") if isinstance(serialized, dict) else ""
        if tool_name == "_Exception":
            self._in_error_retry = True
            return
        self._in_error_retry = False
        self._tools_called += 1
        self._stream_buffer = []       # discard any buffered tokens from the thinking phase
        self._streaming_final = False
        raw = input_str if isinstance(input_str, str) else json.dumps(input_str)
        sql = _clean_sql_input(raw)
        self._consecutive_parse_failures = 0
        # Repetition guard: update the consecutive-identical-query counter so
        # _RobustReActParser._check_repetition can abort a degenerate loop.
        key = ("run_query", sql)
        if key == self._last_action_key:
            self._consecutive_repeats += 1
        else:
            self._last_action_key = key
            self._consecutive_repeats = 1
        self._q.put(("tool_call", {"tool": "run_query", "args": {"sql": sql}, "iteration": self._iteration}))
        self._q.put(("status", "Running query…"))

    def on_tool_end(self, output, **kwargs):
        if self._in_error_retry:
            self._in_error_retry = False
            return
        result_data = self._parse_tool_output(str(output))
        self._q.put(("tool_result", {**result_data, "iteration": self._iteration}))
        self._iteration += 1
        has_problem = bool(result_data.get("error")) or result_data.get("row_count", 0) == 0
        remaining = MAX_ITERATIONS - self._iteration
        if has_problem and remaining > 0:
            self._q.put(("status", f"Retrying… (attempt {self._iteration + 1}/{MAX_ITERATIONS})"))
        else:
            self._q.put(("status", "Formulating answer…"))

    def on_tool_error(self, error, **kwargs):
        result_data = {"row_count": 0, "execution_time_ms": 0, "preview_rows": [], "all_rows": [], "error": str(error)}
        self._q.put(("tool_result", {**result_data, "iteration": self._iteration}))

    def on_llm_new_token(self, token: str, **kwargs) -> None:
        """Forward raw tokens live (thinking_token) so the Agent Activity
        panel streams the model's reasoning in real time, same as `ollama run`.

        Stops forwarding to the raw stream once 'Final Answer:' is seen —
        from that point on the tokens ARE the reply, already streamed
        separately into the chat bubble via _stream_token below. Without this
        cutoff the answer text would appear twice: once raw here, once clean
        in the chat bubble.

        With langchain-ollama >= 0.3 and reasoning enabled, Ollama's thinking
        is surfaced per-chunk in chunk.message.additional_kwargs[
        "reasoning_content"] (not in the token) — stream that live as well.
        """
        if not self._streaming_final:
            chunk = kwargs.get("chunk")
            if chunk is not None:
                msg = getattr(chunk, "message", None)
                if msg is not None:
                    r = (msg.additional_kwargs or {}).get("reasoning_content", "")
                    if r:
                        self._raw_buffer += r
                        now = _time.monotonic()
                        if len(self._raw_buffer) >= 8 or (now - self._last_flush) >= 0.05:
                            self._flush_raw()
                            self._last_flush = now

        if not token:
            return

        if not self._streaming_final:
            # Still in the "thinking" phase for this LLM call — stream raw
            # tokens live into Agent Activity, batched to avoid flooding the
            # UI with a re-render on every single sub-word token.
            self._raw_buffer += token
            now = _time.monotonic()
            if len(self._raw_buffer) >= 8 or (now - self._last_flush) >= 0.05:
                self._flush_raw()
                self._last_flush = now

        if self._tools_called == 0:
            return

        if self._streaming_final:
            self._q.put(("_stream_token", token))
            return

        self._stream_buffer.append(token)
        buf = "".join(self._stream_buffer)
        if "Final Answer:" in buf:
            self._streaming_final = True
            after = buf.split("Final Answer:", 1)[1]
            if after:
                self._q.put(("_stream_token", after))

    def on_agent_finish(self, finish, **kwargs):
        self._flush_raw()
        output = finish.return_values.get("output", "")
        self._q.put(("_output", output))

    def on_chain_error(self, error, **kwargs):
        # OutputParserException is handled by AgentExecutor's handle_parsing_errors —
        # it fires on the inner LLM chain but the outer executor retries.
        # Treating it as fatal here would kill the generator before the retry happens.
        if isinstance(error, OutputParserException):
            return
        self._flush_raw()
        self._q.put(("_error", str(error)))

    def on_chat_model_start(self, serialized, messages, **kwargs):
        """Diagnostic: log how much context reaches the model each call.

        In a healthy ReAct loop the prompt grows every call because the
        agent_scratchpad (all prior Thought/Action/Observation steps) is
        appended. A constant character count across calls would prove the
        scratchpad is NOT being fed back to the model -- the suspected cause
        of granite4.2 re-emitting the same block forever. Pure logging; never
        raises so it cannot affect agent behaviour.
        """
        try:
            chars = 0
            roles = []
            for msg_list in (messages or []):
                for m in msg_list:
                    c = m.content
                    n = len(c) if isinstance(c, str) else len(str(c))
                    chars += n
                    role = getattr(m, "type", None) or type(m).__name__
                    roles.append(f"{role}:{n}")
        except Exception:
            chars, roles = -1, ["?"]
        self._llm_calls += 1
        print(
            f"[local] llm_start call={self._llm_calls} msgs={len(roles)} "
            f"prompt_chars={chars} breakdown=[{', '.join(roles)}]",
            flush=True,
        )


# ── Robust output parser ───────────────────────────────────────────────────────

class _RobustReActParser(ReActSingleInputOutputParser):
    """Extends the default ReAct parser to handle local models that:
    - omit the Action tool name
    - wrap Action Input in code fences
    - write bare prose instead of 'Final Answer:'
    - try to answer without calling any tool (hallucination guard)
    """

    _FINAL_RE = re.compile(r"Final Answer\s*:(.*)\Z", re.DOTALL | re.IGNORECASE)
    _ACTION_RE = re.compile(r"Action\s*:\s*(.*?)(?:\n|$)", re.IGNORECASE)
    _INPUT_RE = re.compile(
        r"Action\s+Input\s*:\s*(.*?)(?:\nObservation:|\nThought:|\Z)",
        re.DOTALL | re.IGNORECASE,
    )

    # Markers used only by _keep_last_turn to locate echoed turns.
    # "Action Input" (not "Action:") so the two are never confused.
    _ACTION_INPUT_START_RE = re.compile(r"Action\s+Input", re.IGNORECASE)
    _ACTION_LINE_START_RE = re.compile(r"Action\s*:", re.IGNORECASE)

    # A "final answer" that is just a bare number/token (e.g. "1") after a
    # multi-row/multi-column tool result almost never a real answer — it's a
    # sign the model's response got truncated or derailed. Deliberately tight
    # (short + only digits/punctuation) so real one-word answers aren't caught.
    _DEGENERATE_FINAL_RE = re.compile(r"^[\d.,\-\s]{1,6}$")

    # Patterns that signal a purely conversational message — no DB query expected.
    _CONVERSATIONAL_RE = re.compile(
        r"^\s*(hi|hello|hey|hallo|bonjour|salut|ciao|thanks?|thank you|danke|merci"
        r"|what can you do|was kannst du|que peux-tu|help|hilfe|aide"
        r"|good\s*(morning|afternoon|evening)|guten\s*(morgen|tag|abend))\W*$",
        re.IGNORECASE,
    )

    # Consecutive unparseable LLM responses (no successful tool execution in
    # between) before the loop breaker aborts the agent. AgentExecutor with
    # handle_parsing_errors=True swallows every OutputParserException and
    # retries — a model stuck in a format loop (observed with
    # granite4.2: `Action: {"sql": ...}` without the `Action Input:` line,
    # ~570 retries in 7 h until context overflow) would otherwise run for
    # hours; AGENT_MAX_ITERATIONS only counts tool executions.
    _MAX_CONSECUTIVE_PARSE_FAILURES = 5

    # Abort when the model is about to run the same query a 3rd time in a row
    # (see _check_repetition). N=2 -> allow two identical executions, stop the
    # third. A genuine transient retry (e.g. a DB blip) stays well under this.
    _MAX_CONSECUTIVE_IDENTICAL = 2

    def __init__(
        self,
        callback: "_EventCallback",
        user_question: str = "",
        tool_names: set[str] | frozenset[str] | None = None,
    ) -> None:
        super().__init__()
        self._cb = callback
        self._user_question = user_question
        self._tool_names = tool_names or {"run_query"}


    def parse_result(
        self, result: list, *, partial: bool = False
    ) -> AgentAction | AgentFinish:
        """Recover native Ollama tool_calls (and inline SQL) before text parse.

        ``gpt-oss:20b`` answers in two ways that the stock ReAct parser drops:

        * empty ``content`` + an Ollama-native ``tool_calls`` array carrying the
          query (langchain-core's ``parse_result`` only forwards the empty
          ``generation.text`` to ``parse()``), and
        * a non-empty ``content`` that is a plain-text reasoning dump — often
          with the full SQL written inline in prose — sometimes accompanied by
          a native ``tool_calls`` entry, sometimes not.

        This override synthesizes the AgentAction in both cases when a valid
        SQL payload can be extracted.  Anything else falls through to the
        stock text path (with its two-strike guard) unchanged.
        """
        if not partial and len(result) == 1 and getattr(result[0], "message", None) is not None:
            msg = result[0].message
            content = msg.content if isinstance(msg.content, str) else ""
            # Recovery only applies while no tool has run yet AND the content
            # is not genuine ReAct text (a model that wrote "Action:" or
            # "Final Answer:" is following the format — let parse() handle it).
            react_markers = re.search(r"(?m)^\s*(Action|Final Answer)\s*:", content)
            if self._cb._tools_called == 0 and not react_markers:
                action = self._action_from_tool_calls(msg)
                if action is None and content.strip():
                    action = self._action_from_embedded_sql(content)
            else:
                action = None
            if action is not None:
                print(
                    "[local] parser: recovered run_query from Ollama message "
                    f"(content_chars={len(content)} tool_calls={len(getattr(msg, 'tool_calls', None) or [])})",
                    flush=True,
                )
                return action
        # Do NOT delegate to super().parse_result: it funnels into parse()
        # and we need parse()'s RuntimeError (parse-loop breaker, raised
        # when parse() gives up) to propagate as-is to the agent executor.
        if partial:
            raise ValueError(
                "Partial parsing is not supported by this parser."
            )
        return self.parse(result[0].text)

    def _record_parse_failure(self, error: OutputParserException) -> None:
        """Count consecutive unparseable LLM responses; abort the agent after
        ``_MAX_CONSECUTIVE_PARSE_FAILURES`` in a row.

        Only ``OutputParserException`` counts: other errors are genuine
        failures (DB down, LLM unreachable) and must keep their normal
        error path. The counter is reset in ``on_tool_start`` when a tool
        actually executes, so the budget is per-format-loop, not per-run.
        The RuntimeError escapes AgentExecutor (which only swallows
        OutputParserException) and is surfaced by the run-thread handler.
        """
        if not isinstance(error, OutputParserException):
            return
        self._cb._consecutive_parse_failures += 1
        if self._cb._consecutive_parse_failures >= self._MAX_CONSECUTIVE_PARSE_FAILURES:
            print(
                f"[local] parser: giving up after "
                f"{self._cb._consecutive_parse_failures} consecutive unparseable "
                "responses (format loop)",
                flush=True,
            )
            raise RuntimeError(
                "The model repeatedly produced a response I could not interpret "
                f"as a valid tool call, even after {self._cb._consecutive_parse_failures} "
                "correction attempts. Please rephrase your question or try a "
                "different model."
            ) from error

    def _action_key(self, action: AgentAction) -> tuple:
        """Normalised (tool, sql) key used by the repetition guard."""
        ti = action.tool_input
        sql = ti if isinstance(ti, str) else json.dumps(ti, ensure_ascii=False)
        return (action.tool, _clean_sql_input(sql))

    def _keep_last_turn(self, text: str) -> str:
        """Trim echoed reasoning history so the parser sees only the last turn.

        Normal output has at most one `Action Input:` (or one `Action:`/
        `Final Answer:`), so this is a no-op and existing behaviour is
        preserved. A model that re-emits its scratchpad (granite4.2) yields
        several of these markers; everything before the last `Action Input:`
        is dead weight that misleads the first-match parsers. We cut at the
        marker *immediately before* the final one, keeping that final marker
        and everything after it.
        """
        if "Action Input" in text:
            starts = [m.start() for m in self._ACTION_INPUT_START_RE.finditer(text)]
            if len(starts) >= 2:
                # Keep only the final "Action Input: ..." onward; the model's
                # current action is always its newest one. The parser recovers
                # the tool name from the JSON/SQL payload, so dropping the
                # preceding "Action:" line is harmless.
                text = text[starts[-1] :]
        elif "Action:" in text:
            starts = [m.start() for m in self._ACTION_LINE_START_RE.finditer(text)]
            if len(starts) >= 2:
                text = text[starts[-1] :]
        return text

    def _check_repetition(self, action: AgentAction) -> None:
        """Abort if the model is about to re-run a query it has already executed
        this many times in a row.

        This is a second degenerate mode, distinct from the parse/format loop
        the _record_parse_failure breaker catches: here every response parses
        and every tool call executes, so no failure is ever recorded. The model
        simply re-issues the identical SQL forever (observed with granite4.2
        at temperature 0.1). State is maintained by the callback
        (on_tool_start), which knows the real executed SQL.
        """
        key = self._action_key(action)
        if (
            key == self._cb._last_action_key
            and self._cb._consecutive_repeats >= self._MAX_CONSECUTIVE_IDENTICAL
        ):
            print(
                f"[local] parser: aborting - same query repeated "
                f"{self._cb._consecutive_repeats + 1}x in a row: {key[1][:80]}",
                flush=True,
            )
            raise RuntimeError(
                "The model kept re-running the exact same query without making "
                "progress, so the agent was stopped. Please rephrase the "
                "question or try a different model."
            )

    def _action_from_tool_calls(self, msg) -> AgentAction | None:
        """Build an AgentAction for run_query from an AIMessage's tool_calls.

        Only accepts a tool named like ``run_query`` (case-insensitive) whose
        argument is recognisably a SQL payload — the same SELECT/WITH
        acceptance test the text recovery paths use. Returns ``None``
        otherwise so the normal parse path (and its guards) takes over.
        """
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            name = str(tc.get("name") or "").lower()
            if "run_query" not in name:
                continue
            args = tc.get("args")
            if isinstance(args, (dict, list)):
                if isinstance(args, dict) and (args.get("sql") or args.get("query")):
                    raw = str(args.get("sql") or args.get("query"))
                else:
                    raw = json.dumps(args, ensure_ascii=False)
            else:
                raw = str(args or "").strip()
                if raw.startswith("{"):
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict) and (parsed.get("sql") or parsed.get("query")):
                            raw = str(parsed.get("sql") or parsed.get("query"))
                    except json.JSONDecodeError:
                        pass
            raw = raw.strip()
            if not raw or len(raw) > 64 * 1024:
                continue
            clean_input = _clean_sql_input(raw)
            if re.match(r"^\s*(SELECT|WITH)\b", clean_input, re.IGNORECASE):
                return AgentAction(tool="run_query", tool_input=clean_input, log=raw)
        return None

    def _action_from_embedded_sql(self, text: str) -> AgentAction | None:
        """Extract a run_query AgentAction from SQL written inline in prose.

        ``gpt-oss:20b`` sometimes emits its whole answer as a reasoning dump
        with the query embedded in the text and no usable tool_call.  We only
        accept a very conservative shape — a SELECT/WITH statement written
        verbatim (possibly multi-clause) and terminated by ``;`` or a
        sentence-ending ``.`` — so legitimate prose that merely mentions SQL
        keywords is never coerced into a query.
        """
        raw = self._extract_inline_sql(text)
        if raw is None:
            return None
        clean_input = _clean_sql_input(raw)
        if re.match(r"^\s*(SELECT|WITH)\b", clean_input, re.IGNORECASE) and len(clean_input) <= 64 * 1024:
            return AgentAction(tool="run_query", tool_input=clean_input, log=clean_input)
        return None

    @staticmethod
    def _extract_inline_sql(text: str) -> str | None:
        """Locate a verbatim SQL statement embedded in prose.

        The statement must start at the beginning of a line or immediately
        after a colon (e.g. ``... So query: SELECT ...``).  Scanning forward
        we stop at the first real terminator: a ``;`` (outside single-quoted
        strings) or a sentence-ending ``.``.  Identifier dots such as
        ``g.geometry`` and decimals are never terminators — a dot only ends
        the statement when it is not flanked by alphanumerics on both sides.
        Returns ``None`` when no anchored statement is found.  A statement
        that runs to the very end of the text without a terminator is only
        accepted when it at least has a FROM clause, so prose that merely
        starts with "SELECT ..." but continues into sentences is rejected.
        """
        start = None
        start_end = None
        for m in re.finditer(r"(?i)\b(SELECT|WITH)\b", text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            prefix = text[line_start:m.start()]
            if prefix.strip() == "" or prefix.rstrip().endswith(":"):
                start = m.start()
                start_end = m.end()
                break
        if start is None:
            return None
        i = start_end
        n = len(text)
        in_str = False
        while i < n:
            c = text[i]
            if in_str:
                if c == "'":
                    if i + 1 < n and text[i + 1] == "'":  # escaped ''
                        i += 2
                        continue
                    in_str = False
            elif c == "'":
                in_str = True
            elif c == ";":
                return text[start:i + 1]
            elif c == ".":
                before = text[i - 1] if i > start else ""
                after = text[i + 1] if i + 1 < n else ""
                if not (before.isalnum() and after.isalnum()):
                    return text[start:i]
            i += 1
        tail = text[start:]
        # No terminator found — only accept a statement that looks complete.
        if re.search(r"\bFROM\b", tail, re.IGNORECASE):
            return tail
        return None

    def _is_conversational(self) -> bool:
        """Return True when the user's input is a greeting or other non-query message."""
        q = self._user_question.strip()
        # Very short messages with no SQL-like keywords are likely conversational.
        if len(q) < 60 and self._CONVERSATIONAL_RE.match(q):
            return True
        return False

    def _require_tool_call(self, text: str) -> None:
        """Raise if the model is trying to answer without having called any tool.

        Skipped entirely for conversational messages (greetings, thanks, etc.).
        On the first violation raise OutputParserException so LangChain can
        send a correction back to the model.  On the second violation allow
        the answer through so the conversation does not loop forever (some
        models ignore the correction and would otherwise output empty strings
        until max_iterations is hit).
        """
        if self._cb._tools_called == 0:
            # Never block a conversational reply — "hello" should not trigger a query.
            if self._is_conversational():
                return
            if self._cb._parse_errors == 0:
                self._cb._parse_errors += 1
                raise OutputParserException(
                    "You have not called run_query yet. "
                    "You MUST call run_query with a SQL SELECT query before giving a Final Answer. "
                    "Do not answer from memory — the data may be different from your training.\n"
                    "Use this format:\n"
                    "Action: run_query\n"
                    'Action Input: {"sql": "SELECT ..."}'
                )
            # Second attempt: model still didn't call the tool — let the answer
            # through to avoid an empty-output loop that never resolves.

    def _coerce_unknown_tool(self, tool_name: str, tool_input) -> AgentAction | None:
        """Recover an AgentAction whose tool name is not a real tool.

        Returns an AgentAction bound to ``run_query`` when ``tool_input`` is
        recognisably a SQL payload, and ``None`` otherwise. ``run_query``
        expects a string, so the input is always normalised to a string here
        (dicts are serialised first), then run through the same
        ``_clean_sql_input`` the rest of the pipeline uses — which extracts
        the bare SQL from JSON ``{"sql": ...}`` payloads and strips markdown
        fences. A SELECT/WITH prefix after cleaning is the acceptance test,
        identical to the existing Action-Input recovery path below.
        """
        if isinstance(tool_input, (dict, list)):
            raw = json.dumps(tool_input, ensure_ascii=False)
        else:
            raw = str(tool_input)
        raw = raw.strip()
        if not raw or len(raw) > 64 * 1024:
            return None
        clean_input = _clean_sql_input(raw)
        if re.match(r"^\s*(SELECT|WITH)\b", clean_input, re.IGNORECASE):
            return AgentAction(tool="run_query", tool_input=clean_input, log=raw)
        return None

    def parse(self, text: str) -> AgentAction | AgentFinish:
        """Wrapper that feeds every failure into the parse-loop breaker.

        All of _parse_impl's failure paths raise OutputParserException
        (unknown tool, no-tool-call strikes, empty output, final give-up).
        Recording here in one place means no raise site is missed; the
        RuntimeError raised by _record_parse_failure passes through
        untouched (it is not an OutputParserException) and aborts the agent.
        """
        try:
            result = self._parse_impl(text)
        except OutputParserException as e:
            self._record_parse_failure(e)
            raise
        # Repetition guard: if the model is about to re-issue a query it has
        # already run several times in a row, abort before it runs again. The
        # RuntimeError raised by _check_repetition is not an
        # OutputParserException, so it is not swallowed above and propagates
        # out of AgentExecutor (same path as the parse-loop breaker).
        if isinstance(result, AgentAction):
            self._check_repetition(result)
        return result

    def _parse_impl(self, text: str) -> AgentAction | AgentFinish:
        # Strip <think>/<thought>/... blocks before any parsing so the standard
        # ReAct parser doesn't choke on Qwen3/DeepSeek/gemma4 reasoning prefixes.
        clean, _reasoning = _strip_reasoning_tags(text)

        # Some models put their entire output inside reasoning tags and leave
        # nothing outside. Fall back to the reasoning text so the
        # Action/SQL/Final-Answer patterns inside it can still be extracted.
        if not clean.strip() and _reasoning.strip():
            clean = _reasoning

        # A degenerate model (granite4.2 at low temperature) can re-emit its
        # whole scratchpad in the response, producing several `Action Input:`
        # lines. The parsers below use re.search() (FIRST match) and would then
        # execute the OLDEST echoed query, silently dropping the model's
        # genuinely-new one. Trim to the last turn before parsing (a no-op for
        # normal single-turn outputs — see _keep_last_turn).
        clean = self._keep_last_turn(clean)

        # Empty output — send one correction, then give up gracefully.
        if not clean.strip():
            self._cb._parse_errors += 1
            if self._cb._parse_errors >= 2:
                return AgentFinish(
                    return_values={
                        "output": (
                            "The model did not produce a response after retrying. "
                            "Try a different model or rephrase your question."
                        )
                    },
                    log=text,
                )
            raise OutputParserException(
                "The model returned an empty response. "
                "Please use the format: Thought / Action: run_query / Action Input: {\"sql\": \"SELECT ...\"}."
            )

        # Try the standard parser first (on the cleaned text)
        try:
            result = super().parse(clean)
        except OutputParserException:
            result = None
        else:
            if isinstance(result, AgentAction) and result.tool not in self._tool_names:
                # Local models (e.g. granite4.2) frequently emit a *sentence* or
                # an invented name as the Action instead of the real tool name.
                # If the input clearly is a SQL payload, coerce to the real tool;
                # otherwise raise so the retry machinery can send a correction.
                coerced = self._coerce_unknown_tool(result.tool, result.tool_input)
                if coerced is not None:
                    print(
                        f"[local] parser: coerced unknown tool '{result.tool}' → run_query",
                        flush=True,
                    )
                    return coerced
                raise OutputParserException(
                    f"Unknown tool name '{result.tool}'. The only available tool is run_query. "
                    "Use this format:\n"
                    "Action: run_query\n"
                    'Action Input: {"sql": "SELECT ..."}'
                )
            return result

        # All subsequent parsing uses the cleaned text.
        text = clean

        # Explicit "Final Answer:" prefix
        fa = self._FINAL_RE.search(text)
        if fa:
            self._require_tool_call(text)
            return AgentFinish(return_values={"output": fa.group(1).strip()}, log=text)

        # `Action: {"sql": ...}` — model put the JSON payload directly after
        # `Action:` without an `Action Input:` line (observed with
        # granite4.2:30b-q4_K_M). The standard parser then reads the whole JSON
        # object as an unknown tool *name*, and the tool-call line is never
        # executed; the correction loop can run for hours. Recover it here.
        # (The Action-Input path below must keep priority — it handles the
        # canonical format, and _ACTION_RE would otherwise mis-match a JSON
        # payload as the tool name.)
        if not self._INPUT_RE.search(text):
            a_line = self._ACTION_RE.search(text)
            if a_line:
                name_part = a_line.group(1).strip()
                if (
                    name_part.startswith("{")
                    and '"sql"' in name_part
                    and len(name_part) <= 64 * 1024
                ):
                    clean_input = _clean_sql_input(name_part)
                    if re.match(r"^\s*(SELECT|WITH)\b", clean_input, re.IGNORECASE):
                        print(
                            "[local] parser: recovered run_query from "
                            "'Action: {json}' line (missing Action Input)",
                            flush=True,
                        )
                        return AgentAction(
                            tool="run_query", tool_input=clean_input, log=text
                        )

        # Action Input present but tool name missing or wrong
        ai_match = self._INPUT_RE.search(text)
        if ai_match:
            raw_input = ai_match.group(1).strip()
            # Strip markdown fences before any JSON/SQL check — models often wrap
            # the action input in ```json ... ``` code blocks.
            clean_input = _clean_sql_input(raw_input)

            a_match = self._ACTION_RE.search(text)
            action_name = (a_match.group(1).strip() if a_match else "").lower()

            # Infer run_query when name is missing/wrong but input has "sql"
            if action_name != "run_query":
                # Guard the JSON parse: hallucinated inputs can be enormous; cap
                # the cost of parsing so we don't OOM or block on garbage.
                try:
                    if len(clean_input) > 64 * 1024:
                        raise ValueError("action input too large to parse as JSON")
                    parsed = json.loads(clean_input)
                    if isinstance(parsed, dict) and "sql" in parsed:
                        action_name = "run_query"
                except (json.JSONDecodeError, TypeError, ValueError):
                    if re.match(r"^\s*(SELECT|WITH)\b", clean_input, re.IGNORECASE):
                        action_name = "run_query"
                    elif '"sql"' in raw_input:
                        # Fenced JSON with sql key that still didn't parse cleanly
                        action_name = "run_query"

            if action_name == "run_query":
                return AgentAction(tool="run_query", tool_input=clean_input, log=text)

        stripped = text.strip()

        # Bare JSON with "sql" key — model skipped the ReAct format entirely.
        if stripped.startswith("{") and len(stripped) <= 64 * 1024:
            try:
                data = json.loads(stripped)
                if isinstance(data, dict) and "sql" in data:
                    return AgentAction(tool="run_query", tool_input=stripped, log=text)
            except json.JSONDecodeError:
                pass

        # Plain prose with no Action/Action Input — model wrote the answer directly.
        # (Common after a simple COUNT result.) Treat it as the final answer.
        if stripped and "Action" not in stripped and "Action Input" not in stripped:
            self._require_tool_call(stripped)

            # Guard against a degenerate bare-token "answer" (e.g. "1") once a
            # tool has already returned real data — same retry-then-give-up
            # pattern as the empty-response guard above, so it doesn't loop
            # forever if the model keeps derailing.
            if self._cb._tools_called > 0 and self._DEGENERATE_FINAL_RE.match(stripped):
                self._cb._parse_errors += 1
                if self._cb._parse_errors >= 2:
                    return AgentFinish(
                        return_values={
                            "output": (
                                f'The model\'s answer was incomplete (just "{stripped}") '
                                "even after retrying. The query itself succeeded — check "
                                "the Agent Activity panel for the raw results, or try "
                                "rephrasing the question."
                            )
                        },
                        log=text,
                    )
                raise OutputParserException(
                    f'Your previous response was incomplete (just "{stripped}"). '
                    "You already have the query result above in the Observation — "
                    "write a complete Final Answer describing it, e.g. "
                    '"Final Answer: <full answer using the data above>".'
                )

            return AgentFinish(return_values={"output": stripped}, log=text)

        raise OutputParserException(f"Could not parse LLM output: `{text}`")


# ── Per-model LLM invocation defaults (plan step c) ───────────────────────────
# Prefix-keyed registry of invocation defaults derived from the local-model
# probe matrix (see production/docs/local-model-probing.md). Longest matching
# prefix wins. Each entry may set any of the user-visible Web UI controls:
#   temperature (float 0..1)           → Temperature slider,  UI default 0.1
#   thinking    (False|None|"low"|"medium"|"high") → Thinking dropdown, UI default "off"
#             (None = omit Ollama's top-level think field entirely; needed by
#             gpt-oss, where think:false measurably degrades tool-calling)
#   num_ctx     (int tokens)           → Context window dropdown, UI default 65536
# (num_predict stays env-driven via LOCAL_MAX_TOKENS; reserved for future
# entries but not part of _resolve_llm_kwargs yet.)
#
# _resolve_llm_kwargs() applies an entry's values ONLY where the user left the
# corresponding UI control at its default. Explicit user values always win.

# The Web UI control defaults — a control sitting at these values is treated
# as "the user did not touch it". Keep in sync with app.py (temperature
# slider value=0.1, thinking dropdown value="off", num_ctx "64K (65,536)").
_UI_DEFAULTS = {
    "temperature": 0.1,
    "thinking": False,
    "num_ctx": 65536,
}

_MODEL_KWARG_DEFAULTS: dict[str, dict] = {
    # gpt-oss:20b answers with empty content and delivers the query as an
    # Ollama-native tool_call (see _RobustReActParser.parse_result, which
    # recovers it). think MUST be omitted entirely (None), not false:
    # measured through the real langchain path (35k-char system prompt,
    # streaming) — think:false 3/6 recovered (incl. instant no-tool
    # hallucinations), think:null 4/4, think:true 0/4; raw-Ollama probes
    # (no think field) 10/10. A cool temperature keeps the runs short and
    # stable.
    "gpt-oss": {"temperature": 0.1, "thinking": None},
    # thinking-then-empty class: Ollama 0.33.x gemma4 bug — a full query plan
    # is produced in the reasoning stream, then content stays empty. Keeping
    # thinking off is the best available configuration for these two models.
    "gemma4:26b": {"thinking": False},
    "gemma4:31b": {"thinking": False},
}


def _resolve_llm_kwargs(
    model: str,
    temperature: float | None,
    enable_thinking: bool | str | None,
    num_ctx: int | None,
) -> tuple[float, bool | str, int | None]:
    """Apply per-model invocation defaults where the user did not override.

    Returns the (possibly replaced) (temperature, enable_thinking, num_ctx)
    triple to pass to ChatOllama. A model default is applied only when the
    corresponding UI control is still at its Web UI default (see
    _UI_DEFAULTS); any explicit user value always wins. Unknown models pass
    through unchanged. Note: enable_thinking may be None (omit Ollama's
    think field); the "untouched UI" comparison is identity-based so the
    profile's None is distinguishable from the UI's False.
    """
    m = model.lower()
    spec: dict = {}
    best_len = 0
    for prefix in _MODEL_KWARG_DEFAULTS:
        if m.startswith(prefix) and len(prefix) > best_len:
            spec = _MODEL_KWARG_DEFAULTS[prefix]
            best_len = len(prefix)

    if not spec:
        return temperature, enable_thinking, num_ctx

    if temperature is not None and "temperature" in spec and abs(temperature - _UI_DEFAULTS["temperature"]) < 1e-9:
        temperature = spec["temperature"]
    # Identity (is) not equality (==): False is not identity-equal to the
    # UI default only when the profile already replaced it, and a profile
    # value of None must survive as None.
    if "thinking" in spec and enable_thinking is _UI_DEFAULTS["thinking"]:
        enable_thinking = spec["thinking"]
    if "num_ctx" in spec and num_ctx == _UI_DEFAULTS["num_ctx"]:
        num_ctx = spec["num_ctx"]
    return temperature, enable_thinking, num_ctx


# ── Main entry point ───────────────────────────────────────────────────────────

def react_stream(
    provider: str,
    model: str,
    temperature: float | None,
    messages: list[dict],
    tool_executor: Callable[[str], str],
    *,
    enable_thinking: bool | str | None = False,
    num_ctx: int | None = None,
) -> Generator[tuple, None, None]:
    """LangChain ReAct agent for Ollama.

    enable_thinking: False disables reasoning; True enables it at the model's
    default depth; a string ("low"/"medium"/"high") sets an explicit thinking
    level; None (set per-model by _resolve_llm_kwargs, e.g. for gpt-oss)
    omits Ollama's top-level "think" field entirely. All of these are
    forwarded via ChatOllama's `reasoning` kwarg (langchain-ollama >= 0.3).
    The ollama client that langchain-ollama 0.3.x pins only accepts
    bool/None/low/medium/high, so "max" is not a valid level and must not
    be passed here.
    """

    # ── Build the LangChain LLM ────────────────────────────────────────────────
    # Per-model invocation defaults (plan step c): only fill in controls the
    # user left at their Web UI default. User overrides always win.
    temperature, enable_thinking, num_ctx = _resolve_llm_kwargs(
        model, temperature, enable_thinking, num_ctx
    )

    from langchain_ollama import ChatOllama
    # num_ctx goes in model_kwargs (→ Ollama options field)
    # reasoning goes as a top-level ChatOllama kwarg (→ Ollama "think" field, not options)
    _ctx = num_ctx if num_ctx is not None else int(os.environ.get("OLLAMA_NUM_CTX", "32768"))
    ollama_init: dict = dict(
        model=model,
        base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        timeout=float(os.environ.get("OLLAMA_TIMEOUT", "300")),
        num_predict=int(os.environ.get("LOCAL_MAX_TOKENS", "16000")),
        streaming=True,
        model_kwargs={"num_ctx": _ctx},
        reasoning=enable_thinking,
    )
    if temperature is not None:
        ollama_init["temperature"] = temperature
    llm = ChatOllama(**ollama_init)

    # ── Build the run_query tool ───────────────────────────────────────────────
    def _run_query(input_data: str) -> str:
        """Execute SQL and return a human-readable observation (same format as old demo)."""
        sql = _clean_sql_input(input_data)
        print(f"[local] run_query → {sql[:120]}", flush=True)
        try:
            raw = str(tool_executor(sql))
            parsed = _parse_tool_result(raw)

            if parsed.get("error"):
                return f"Error: {parsed['error']}. Fix the SQL and try again."

            rows = parsed.get("all_rows") or parsed.get("preview_rows") or []
            count = parsed.get("row_count", len(rows))

            if count == 0:
                return (
                    "No results found. Your WHERE conditions are probably wrong. "
                    "Common causes: function values are stored as numeric codes "
                    "(e.g. '31001_1000' not 'Wohngebäude'), street names need "
                    "ILIKE '%%Röblingweg%%'. Try a simpler query first."
                )

            # Format like old demo: one JSON object per line
            lines = [json.dumps(row, ensure_ascii=False) for row in rows[:50]]
            result_str = "\n".join(lines)
            truncation_note = f" (showing first 50 of {count})" if count > 50 else ""
            result_msg = (
                f"The query was executed successfully. "
                f"TOTAL ROWS RETURNED: {count}{truncation_note}. "
                f"Use this exact number in your answer — do NOT count the rows yourself. "
                f"The result is:\n{result_str}"
            )
            # Detect semantically empty aggregate (all zeros / all nulls).
            # This typically means a missing CG_MakeSolid wrapper or a wrong join.
            from webui.llm_utils import _is_all_null_result
            if _is_all_null_result(parsed):
                result_msg += (
                    "\n\nWARNING: All returned values are zero or NULL. "
                    "This usually means the query logic is wrong, not that the data is absent. "
                    "Common fixes: (1) wrap geometry with CG_MakeSolid before CG_Volume, "
                    "(2) check that your JOIN conditions match actual data, "
                    "(3) verify the geometry type filter (IN (8, 9)) is correct. "
                    "Retry with a corrected query."
                )
            # Keep the language reminder as the very last line of every observation
            # so it's the freshest text the model reads before writing Final Answer.
            result_msg += (
                "\n\n[REMINDER] Answer in the user's language. "
                "English question → English answer. Never switch languages.\n"
                "WRONG: User asks 'which objectclass has CompositeSolid geometry?' "
                "→ 'Die Objektklasse mit CompositeSolid-Geometrie ist Gebäude.'\n"
                "RIGHT: User asks 'which objectclass has CompositeSolid geometry?' "
                "→ 'The objectclass with CompositeSolid geometry is Building (objectclass_id 901).'"
            )
            return result_msg
        except Exception as exc:
            return f"Error executing query: {exc}"

    sql_tool = Tool(
        name="run_query",
        func=_run_query,
        description=(
            "Execute a read-only SQL SELECT query against the connected 3DCityDB database "
            "and return the results as JSON. "
            'Input: a JSON object with a single key "sql" whose value is the SQL string, '
            'e.g. {"sql": "SELECT f.objectid FROM feature f WHERE f.objectclass_id = 901"}'
        ),
    )

    # ── Build the system prompt ────────────────────────────────────────────────
    if _USE_STATIC_PROMPT:
        system_content = _STATIC_PROMPT
    else:
        system_content = "\n\n".join(
            m.get("content", "") for m in messages if m["role"] == "system"
        )
    # The template below is a ChatPromptTemplate, so any stray `{...}` in the
    # (dynamic) MCP system content would be parsed as a template variable and
    # break agent construction. Escape literal braces before embedding it.
    system_content = _escape_template_braces(system_content)

    # Last user question
    _raw_user_q = next(
        (m.get("content", "") for m in reversed(messages) if m["role"] == "user"), ""
    )

    # /no_think soft switch for Qwen3 etc.
    # Also inject a per-message language reminder so the model doesn't default
    # to German when the database content is German but the question is not.
    _lang_reminder = (
        "\n\n[INSTRUCTION: Write your Final Answer ONLY in the same language as the "
        "question above. Detect the language from the words I used, NOT from the "
        "database content or street/place names. Do not switch to German or any "
        "other language regardless of what the data looks like.]"
    )
    user_q = _raw_user_q + _lang_reminder
    if not enable_thinking and not user_q.endswith("/no_think"):
        user_q = user_q + "\n\n/no_think"

    # Convert prior user/assistant turns to LangChain message objects.
    # Skip the last user message — it becomes {input}. Compare against raw content
    # (before the /no_think suffix was appended) so the filter is reliable.
    chat_history: list = []
    _seen_last_user = False
    for m in reversed(messages):
        if m["role"] == "user" and not _seen_last_user and m.get("content") == _raw_user_q:
            _seen_last_user = True  # skip this one — it's {input}
            continue
        if m["role"] == "user" and m.get("content"):
            chat_history.insert(0, HumanMessage(content=m["content"]))
        elif m["role"] == "assistant" and m.get("content"):
            chat_history.insert(0, AIMessage(content=m["content"]))

    # Build the prompt using ChatPromptTemplate + MessagesPlaceholder so the agent
    # sees the full conversation history, matching the old_ollama_demo.py approach.
    react_prompt = ChatPromptTemplate.from_messages([
        ("system",
            system_content
            + "\n\nYou have access to the following tools:\n{tools}\n\n"
            "Available tool names: {tool_names}\n\n"
            "Follow this format strictly:\n"
            "Thought: what you are thinking\n"
            "Action: the action to take, should be one of [{tool_names}]\n"
            'Action Input: {{"sql": "SELECT ..."}}\n'
            "Observation: the result of the action\n"
            "... (Thought/Action/Action Input/Observation can repeat as needed)\n"
            "Thought: I now know the final answer\n"
            "LANGUAGE RULE (mandatory): Detect the language from the USER'S WORDS, "
            "not from the database content (which may be in German regardless of the question). "
            "Your Final Answer MUST be in that language.\n"
            "Final Answer: the final answer to the original question"
        ),
        MessagesPlaceholder("chat_history"),
        ("human", "{input}"),
        ("assistant", "Thought:{agent_scratchpad}"),
    ])

    # ── Create callback first so the parser can reference it ──────────────────
    event_q: queue.Queue = queue.Queue()
    callback = _EventCallback(event_q)

    # ── Build the agent executor ───────────────────────────────────────────────
    agent = create_react_agent(llm, [sql_tool], react_prompt, output_parser=_RobustReActParser(callback, user_question=_raw_user_q, tool_names={sql_tool.name}))
    agent_executor = AgentExecutor(
        agent=agent,
        tools=[sql_tool],
        verbose=True,
        max_iterations=MAX_ITERATIONS,
        early_stopping_method="generate",
        handle_parsing_errors=True,
        return_intermediate_steps=False,
    )

    # ── Context usage (approximate — system + conversation) ───────────────────
    _used_tok, _ctx_limit, _ = _log_context_usage(provider, model, messages, num_ctx=num_ctx)
    yield ("context_update", {"used": _used_tok, "limit": _ctx_limit})
    yield ("status", "Reasoning…")

    def _run_agent():
        try:
            agent_executor.invoke(
                {"input": user_q, "chat_history": chat_history},
                config={"callbacks": [callback]},
            )
        except Exception as exc:
            event_q.put(("_error", str(exc)))
        finally:
            callback._flush_raw()
            event_q.put((_SENTINEL, None))

    t = threading.Thread(target=_run_agent, daemon=True)
    t.start()

    # ── Consume events from the queue ─────────────────────────────────────────
    output_text = ""
    streaming_started = False
    while True:
        if _is_stopped():
            yield ("stopped", "")
            return

        try:
            event_type, data = event_q.get(timeout=0.15)
        except queue.Empty:
            yield ("ping", "")
            continue

        if event_type is _SENTINEL:
            break

        if event_type == "_output":
            output_text = data

        elif event_type == "_stream_token":
            # Live token from the final answer — strip <think> tags on the fly
            # and forward clean tokens so the user sees the answer being written.
            if not streaming_started:
                streaming_started = True
            # Don't stream thinking tokens (inside <think>...</think>)
            # Simple heuristic: skip if token contains tag markers
            if "<think>" not in data and "</think>" not in data:
                yield ("final", data)

        elif event_type == "_error":
            print(f"[local] agent error: {data}", flush=True)
            yield ("final", f"Agent error: {data}")
            return

        elif event_type in ("tool_call", "tool_result", "status", "thinking", "thinking_token"):
            yield (event_type, data)

        # ignore unknown internal events

    # ── Emit the final answer ──────────────────────────────────────────────────
    # If streaming was active, tokens were already yielded live — just emit
    # reasoning extracted from the full output if any, but skip re-sending text.
    if output_text:
        visible, reasoning = _strip_reasoning_tags(output_text)
        if reasoning:
            yield ("thinking", reasoning)
        if not streaming_started:
            # Non-streaming path: emit the complete answer now
            final_text = _post_process_markdown(visible)
            if final_text:
                yield ("final", final_text)
    elif not streaming_started:
        yield ("final", "_No answer produced. Try rephrasing or selecting a different model._")
