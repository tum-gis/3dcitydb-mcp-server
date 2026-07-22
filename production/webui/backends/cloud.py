"""Native tool-calling backend for Anthropic and OpenAI providers."""

import json
import time as _time
from typing import Callable, Generator

from webui.llm_utils import (
    MAX_ITERATIONS,
    RUN_QUERY_TOOL,
    _SQL_FENCE_RE,
    _cancel_active_future,
    _executor,
    _is_all_null_result,
    _is_stopped,
    _litellm_kwargs,
    _log_context_usage,
    safe_completion,
    _parse_tool_result,
    _post_process_markdown,
    _set_active_future,
    _stop_event,
    _strip_reasoning_tags,
    _truncate_result,
)


def native_tool_stream(
    provider: str,
    model: str,
    temperature: float,
    messages: list[dict],
    tool_executor: Callable[[str], str],
    *,
    enable_thinking: bool = True,  # unused for cloud, kept for API parity
) -> Generator[tuple, None, None]:
    """Agentic loop using native tool calling (Anthropic, OpenAI).

    Yields the shared event protocol tuples defined in events.py.
    """
    kw = _litellm_kwargs(provider, model, temperature)

    last_error = ""
    last_had_error = False
    force_text_response = False
    _last_user_q = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""
    )

    working_messages = list(messages)

    # Track content from a previous iteration to detect redundant tool calls.
    _cached_content: str = ""
    _last_tool_sql: str = ""

    for iteration in range(MAX_ITERATIONS):
        if _is_stopped():
            print("[cloud] stop flag set at iteration boundary — exiting", flush=True)
            yield ("stopped", "")
            return

        if iteration == 0:
            _status = "Thinking…"
        elif last_had_error:
            _status = f"Retrying… (attempt {iteration + 1}/{MAX_ITERATIONS})"
        else:
            _status = "Formulating answer…"

        print(
            f"[cloud] iteration {iteration}: calling {kw['model']} "
            f"({len(working_messages)} msgs, force_text={force_text_response})",
            flush=True,
        )
        _used_tok, _ctx_limit, _ = _log_context_usage(provider, model, working_messages)
        yield ("context_update", {"used": _used_tok, "limit": _ctx_limit})

        if force_text_response:
            _fut = _executor.submit(
                safe_completion,
                kw,
                messages=working_messages,
                tools=[RUN_QUERY_TOOL],
                tool_choice="none",
            )
        else:
            _fut = _executor.submit(
                safe_completion,
                kw,
                messages=working_messages,
                tools=[RUN_QUERY_TOOL],
                tool_choice="auto",
            )
        _set_active_future(_fut)
        try:
            yield ("status", _status)
            while not _fut.done():
                if _stop_event.is_set():
                    print("[cloud] stop requested — exiting loop", flush=True)
                    yield ("stopped", "")
                    return
                yield ("ping", "")
                _time.sleep(0.25)

            try:
                response = _fut.result()
            except Exception as exc:
                print(f"[cloud] ERROR: {type(exc).__name__}: {exc}", flush=True)
                raise
        finally:
            _set_active_future(None)

        msg = response.choices[0].message
        content = msg.content or ""
        tool_calls = msg.tool_calls or []

        print(
            f"[cloud] iteration {iteration}: content={len(content)} chars, "
            f"tool_calls={len(tool_calls)}",
            flush=True,
        )

        if not tool_calls:
            if not force_text_response:
                sql_match = _SQL_FENCE_RE.search(content)
            else:
                sql_match = None

            if sql_match:
                sql = sql_match.group(1).strip()
                print(f"[cloud] SQL-in-text fallback: {sql[:120]}", flush=True)
                yield ("tool_call", {"tool": "run_query", "args": {"sql": sql}, "iteration": iteration})
                try:
                    raw_result = str(tool_executor(sql))
                    result_data = _parse_tool_result(raw_result)
                    last_error = result_data.get("error") or ""
                except Exception as exc:
                    last_error = str(exc)
                    raw_result = f"Error: {last_error}"
                    result_data = {"row_count": 0, "execution_time_ms": 0, "preview_rows": [], "all_rows": [], "error": last_error}

                yield ("tool_result", {**result_data, "iteration": iteration})
                working_messages.append({"role": "assistant", "content": content})
                remaining = MAX_ITERATIONS - iteration - 1
                if last_error:
                    last_had_error = True
                    fb = (
                        f"Tool call failed with error: {last_error}\n"
                        f"The SQL was: {sql}\n"
                        f"Analyze what went wrong and try a corrected query. "
                        f"You have {remaining} attempts remaining."
                    )
                elif result_data.get("row_count", 0) > 0 and not _is_all_null_result(result_data):
                    last_had_error = False
                    force_text_response = True
                    fb = (
                        "--- FRESH TOOL RESULT FOR CURRENT USER QUESTION ---\n"
                        + _truncate_result(raw_result)
                        + "\n--- END FRESH TOOL RESULT ---\n"
                        "Answer the CURRENT user question using ONLY the rows above. "
                        "Do not reuse wording from any previous turn."
                    )
                elif result_data.get("row_count", 0) > 0:
                    last_had_error = True
                    fb = (
                        f"The query returned {result_data['row_count']} row(s) but every value "
                        f"is NULL. An aggregate function found no matching rows after the JOINs "
                        f"and WHERE filters. Try a different approach: simplify the query, "
                        f"verify geometry type values or LOD, or run a plain SELECT first to "
                        f"confirm the data exists. "
                        f"You have {remaining} attempts remaining."
                    )
                else:
                    last_had_error = True
                    fb = (
                        f"Query returned 0 rows. The conditions or column names may be wrong. "
                        f"Try a different SQL approach. "
                        f"You have {remaining} attempts remaining."
                    )
                working_messages.append({"role": "user", "content": fb})
                continue

            # Pure text — final answer
            if content:
                print("[cloud] final text response", flush=True)
                visible, reasoning = _strip_reasoning_tags(content)
                if reasoning:
                    yield ("thinking", reasoning)
                final_text = _post_process_markdown(visible)
                if final_text:
                    yield ("final", final_text)
            else:
                print("[cloud] WARNING: empty response (no text, no tool calls)", flush=True)
            return

        # Append assistant message (with tool_calls) to history
        working_messages.append(msg)

        # Emit model reasoning alongside tool call.
        # When the content is substantial (>50 chars), treat it as a thinking event.
        # Also cache it in case the next iteration makes a redundant identical call.
        if content:
            if len(content) > 50:
                _cached_content = content
            yield ("thinking", content)

        for tc in tool_calls:
            if _is_stopped():
                print("[cloud] stop flag set before tool exec — exiting", flush=True)
                yield ("stopped", "")
                return

            sql = ""
            raw_result = ""
            try:
                raw_args = tc.function.arguments or ""
                # Defensive size cap: a well-formed tool call is rarely >32 KB;
                # anything bigger is an LLM hallucinating JSON. Reject early.
                if len(raw_args) > 32 * 1024:
                    raise ValueError(
                        f"tool arguments too large ({len(raw_args)} bytes)"
                    )
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError as je:
                    raise ValueError(f"malformed tool arguments: {je}") from je
                if not isinstance(args, dict) or "sql" not in args:
                    raise ValueError(
                        "tool arguments must be an object with a 'sql' key"
                    )
                sql = args.get("sql") or ""
                if not isinstance(sql, str) or not sql.strip():
                    raise ValueError("tool argument 'sql' must be a non-empty string")
                print(f"[cloud] run_query → {sql[:120]}", flush=True)

                # Detect redundant query: same SQL as previous iteration → emit cached content.
                _sql_norm = sql.strip().lower()
                if (
                    _cached_content
                    and _last_tool_sql
                    and _sql_norm == _last_tool_sql
                ):
                    print("[cloud] redundant SQL detected — emitting cached content as final", flush=True)
                    visible, reasoning = _strip_reasoning_tags(_cached_content)
                    if reasoning:
                        yield ("thinking", reasoning)
                    final_text = _post_process_markdown(visible)
                    if final_text:
                        yield ("final", final_text)
                    return

                _last_tool_sql = _sql_norm
                yield ("tool_call", {"tool": "run_query", "args": {"sql": sql}, "iteration": iteration})
                raw_result = str(tool_executor(sql))
                result_data = _parse_tool_result(raw_result)
                last_error = result_data.get("error") or ""
                print(f"[cloud] result → {raw_result[:300]}", flush=True)
            except Exception as exc:
                last_error = str(exc)
                raw_result = f"Error: {last_error}"
                result_data = {"row_count": 0, "execution_time_ms": 0, "preview_rows": [], "all_rows": [], "error": last_error}
                print(f"[cloud] tool_executor ERROR: {type(exc).__name__}: {exc}", flush=True)

            yield ("tool_result", {**result_data, "iteration": iteration})

            remaining = MAX_ITERATIONS - iteration - 1
            if last_error:
                last_had_error = True
                tool_content = (
                    f"Tool call failed with error: {last_error}\n"
                    f"The SQL was: {sql}\n"
                    f"Analyze what went wrong and try a corrected query. "
                    f"You have {remaining} attempts remaining."
                )
            elif result_data.get("row_count", 0) > 0 and not _is_all_null_result(result_data):
                last_had_error = False
                force_text_response = True
                _cached_content = ""  # fresh result; clear cached reasoning
                tool_content = (
                    "--- FRESH TOOL RESULT FOR CURRENT USER QUESTION ---\n"
                    + _truncate_result(raw_result)
                    + "\n--- END FRESH TOOL RESULT ---\n"
                    f"The user's question was:\n\"\"\"\n{_last_user_q}\n\"\"\"\n"
                    "Reply in EXACTLY the same language as the user's question above "
                    "(English → English, German → German). Ignore foreign-language proper "
                    "nouns (street names, place names) when deciding the language.\n"
                    "Answer the CURRENT user question using ONLY the rows above. "
                    "Do not reuse wording from any previous turn."
                )
            elif result_data.get("row_count", 0) > 0:
                last_had_error = True
                tool_content = (
                    f"The query returned {result_data['row_count']} row(s) but every value "
                    f"is NULL. An aggregate function (SUM/AVG/MAX/…) found no matching rows "
                    f"after the JOINs and WHERE filters. Try a different approach: simplify "
                    f"the query, verify the geometry type values or LOD, or run a plain "
                    f"SELECT first to confirm the data exists. "
                    f"You have {remaining} attempts remaining."
                )
            else:
                last_had_error = True
                tool_content = (
                    f"Query returned 0 rows. The conditions or column names may be wrong "
                    f"(e.g. string values may be stored as codes, not English words). "
                    f"You MUST call run_query again with a different SQL approach — "
                    f"do NOT give a final answer yet. "
                    f"You have {remaining} attempts remaining."
                )

            working_messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": tool_content}
            )

            if _is_stopped():
                print("[cloud] stop flag set after tool exec — exiting", flush=True)
                yield ("stopped", "")
                return

        print(f"[cloud] tool done, looping iteration {iteration + 1}", flush=True)

    # All iterations exhausted — compose a graceful failure message.
    error_hint = f" Last error: {last_error}." if last_error else ""
    working_messages.append({
        "role": "user",
        "content": (
            f"You have tried {MAX_ITERATIONS} SQL queries but none returned usable results."
            f"{error_hint} "
            f"Respond in the SAME LANGUAGE the user used in their original question. "
            f"Briefly apologise, explain that the data could not be retrieved, and suggest "
            f"they try rephrasing or verify the data exists. Do NOT include any SQL."
        ),
    })
    _fut_fail = _executor.submit(
        safe_completion,
        kw,
        messages=working_messages,
        tools=[RUN_QUERY_TOOL],
        tool_choice="none",
    )
    _set_active_future(_fut_fail)
    try:
        yield ("status", "Composing response…")
        while not _fut_fail.done():
            if _stop_event.is_set():
                yield ("stopped", "")
                return
            yield ("ping", "")
            _time.sleep(0.25)
        try:
            r_fail = _fut_fail.result()
        except Exception:
            yield ("final", f"I tried {MAX_ITERATIONS} times but couldn't complete this.{error_hint} Could you try rephrasing the question?")
            return
    finally:
        _set_active_future(None)

    fail_answer = r_fail.choices[0].message.content or ""
    if fail_answer:
        visible, reasoning = _strip_reasoning_tags(fail_answer)
        if reasoning:
            yield ("thinking", reasoning)
        final_text = _post_process_markdown(visible)
        if final_text:
            yield ("final", final_text)
