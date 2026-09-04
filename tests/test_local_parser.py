"""Unit tests for _RobustReActParser: unknown-tool coercion, the
`Action: {"sql": ...}` (missing Action Input) granite-4.2 recovery, and the
consecutive parse-failure loop breaker.

Run from the repo root (host):
    python tests/test_local_parser.py
or inside the agent image (has langchain):
    docker run --rm -v <repo>:/repo citydb-mcp-agent:local python /repo/tests/test_local_parser.py
"""

import json
import os
import queue
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

from langchain_core.agents import AgentAction, AgentFinish  # noqa: E402
from langchain_core.exceptions import OutputParserException  # noqa: E402

from webui.backends.local import (  # noqa: E402
    _RobustReActParser,
    _escape_template_braces,
    _clean_sql_input,
)


def _exec_sql(result) -> str:
    """Return the SQL the tool would actually execute, mirroring
    _run_query's use of _clean_sql_input. Normalises both the raw-JSON form
    (standard ReAct path) and the already-cleaned form (fallback path)."""
    ti = result.tool_input
    return _clean_sql_input(json.dumps(ti, ensure_ascii=False) if isinstance(ti, (dict, list)) else ti).strip()


class _StubCallback:
    """Mimics the subset of _EventCallback the parser (and loop breaker) touches."""

    def __init__(self) -> None:
        self._tools_called = 0
        self._parse_errors = 0
        self._consecutive_parse_failures = 0
        self._last_action_key = None
        self._consecutive_repeats = 0
        self._llm_calls = 0


def _parser(tool_names=None, tools_called=0) -> tuple[_RobustReActParser, _StubCallback]:
    cb = _StubCallback()
    cb._tools_called = tools_called
    p = _RobustReActParser(cb, tool_names=tool_names)
    return p, cb


def test_granite_sentence_as_tool_name_sql_json() -> None:
    """granite4.2 failure mode: a sentence as the Action name, SQL JSON as input."""
    p, _ = _parser()
    text = (
        "Thought: I need the building volumes.\n"
        "Action: Run a database query to get the volumes\n"
        'Action Input: {"sql": "SELECT f.objectid, CG_Volume(g.geometry) AS vol '
        'FROM feature f JOIN geometry_data g ON g.feature_id = f.id '
        'WHERE f.objectclass_id = 901"}\n'
    )
    result = p.parse(text)
    assert isinstance(result, AgentAction)
    assert result.tool == "run_query"
    # _clean_sql_input normalises the JSON payload to bare SQL.
    assert result.tool_input.startswith("SELECT")
    assert "objectclass_id = 901" in result.tool_input


def test_unknown_tool_bare_sql_coerced() -> None:
    p, _ = _parser()
    text = (
        "Action: get_volumes\n"
        "Action Input: SELECT f.objectid FROM feature f WHERE f.objectclass_id = 901\n"
    )
    result = p.parse(text)
    assert isinstance(result, AgentAction)
    assert result.tool == "run_query"
    assert result.tool_input.startswith("SELECT")


def test_unknown_tool_nonsensical_input_raises() -> None:
    """Unknown tool + input that is not SQL → raise so retry machinery corrects."""
    p, _ = _parser()
    text = (
        "Action: check_the_weather\n"
        'Action Input: "I do not know how to answer this"\n'
    )
    try:
        p.parse(text)
        assert False, "expected OutputParserException"
    except OutputParserException as e:
        assert "Unknown tool name" in str(e)


def test_real_tool_name_passes_through_unchanged() -> None:
    p, _ = _parser()
    text = (
        "Action: run_query\n"
        'Action Input: {"sql": "SELECT 1"}\n'
    )
    result = p.parse(text)
    assert isinstance(result, AgentAction)
    assert result.tool == "run_query"
    # Standard-parser path keeps the raw (JSON) input; _run_query re-cleans it.
    assert "SELECT 1" in result.tool_input


def test_custom_tool_names_respected() -> None:
    """When the agent has other tools, an unknown name + non-SQL input
    raises; a real extra tool name passes through untouched; SQL input under
    an unknown name still coerces to run_query."""
    p, _ = _parser(tool_names={"run_query", "get_weather"})
    # Real extra tool → no coercion, passes through as-is.
    result = p.parse("Action: get_weather\nAction Input: Rotterdam\n")
    assert isinstance(result, AgentAction)
    assert result.tool == "get_weather"
    # Unknown tool + non-SQL input → raise so the retry machinery corrects.
    p2, _ = _parser(tool_names={"run_query", "get_weather"})
    try:
        p2.parse("Action: something_else\nAction Input: I do not know\n")
        assert False, "expected OutputParserException"
    except OutputParserException:
        pass
    # SQL input under an unknown name still coerces to run_query.
    p3, _ = _parser(tool_names={"run_query", "get_weather"})
    result = p3.parse(
        "Action: something_else\nAction Input: {\"sql\": \"SELECT 2\"}\n"
    )
    assert isinstance(result, AgentAction)
    assert result.tool == "run_query"


def test_final_answer_unaffected() -> None:
    p, _ = _parser(tools_called=1)
    text = "Thought: done\nFinal Answer: 13 buildings were found."
    result = p.parse(text)
    assert isinstance(result, AgentFinish)
    assert "13 buildings" in result.return_values["output"]


def test_dict_tool_input_serialised() -> None:
    """If a subclass ever passes a dict tool_input, coercion stringifies it."""
    p, _ = _parser()
    action = AgentAction(
        tool="Run the query",
        tool_input={"sql": "SELECT 3"},
        log="x",
    )
    coerced = p._coerce_unknown_tool(action.tool, action.tool_input)
    assert coerced is not None
    assert coerced.tool == "run_query"
    assert coerced.tool_input.startswith("SELECT 3")


# ── Fix A: `Action: {"sql": ...}` without an `Action Input:` line (granite4.2) ──


def test_action_json_no_input_line_recovered() -> None:
    """granite4.2:30b-q4_K_M emitted the JSON payload directly after Action:.

    The stock parser then treats the whole JSON object as an unknown tool
    name; the recovery block must turn it into a real run_query action with
    bare SQL as the input.
    """
    p, cb = _parser()
    text = (
        'Thought: I need the part count.\nAction: {"sql": "SELECT COUNT(*) AS part_count '
        'FROM feature WHERE objectclass_id = 902;"}\n'
    )
    result = p.parse(text)
    assert isinstance(result, AgentAction)
    assert result.tool == "run_query"
    assert result.tool_input.startswith("SELECT")
    assert "objectclass_id = 902" in result.tool_input
    assert cb._consecutive_parse_failures == 0


def test_action_json_trailing_backtick_recovered() -> None:
    """Same granite shape with the trailing backtick seen in the 09-01 logs."""
    p, _ = _parser()
    text = (
        'Thought: count the parts.\nAction: {"sql": "SELECT COUNT(*) AS part_count '
        'FROM feature WHERE objectclass_id = 902;"}`\n'
    )
    result = p.parse(text)
    assert isinstance(result, AgentAction)
    assert result.tool == "run_query"
    assert result.tool_input.startswith("SELECT")


def test_action_json_multiline_not_recovered() -> None:
    """A multi-line JSON payload after `Action:` does not match the single-line
    Action regex — it falls through to the loop-breaker path (raises), which
    is the documented, acceptable behaviour."""
    p, _ = _parser()
    text = 'Action: {\n  "sql": "SELECT 1"\n}\n'
    try:
        p.parse(text)
        assert False, "expected OutputParserException"
    except OutputParserException:
        pass


def test_action_json_non_sql_raises() -> None:
    """`Action: {"notsql": ...}` must NOT be coerced into a query."""
    p, _ = _parser()
    text = 'Action: {"notsql": "y"}\n'
    try:
        p.parse(text)
        assert False, "expected OutputParserException"
    except OutputParserException:
        pass


def test_action_input_line_keeps_priority() -> None:
    """When a real `Action Input:` line exists, the canonical path wins over
    the Fix-A recovery (the recovery block must be skipped entirely)."""
    p, _ = _parser()
    text = (
        'Action: {"sql": "SELECT 1"}\n'
        'Action Input: {"sql": "SELECT 2"}\n'
    )
    result = p.parse(text)
    assert isinstance(result, AgentAction)
    assert result.tool == "run_query"
    assert "SELECT 2" in result.tool_input
    assert "SELECT 1" not in result.tool_input


# ── Fix B: consecutive parse-failure loop breaker ─────────────────────────────

# A genuinely unparseable shape (unknown tool name + non-SQL input) — used for
# every parse in a row. (The granite `Action: {"sql": ...}` shape is no longer
# a parse failure: Fix A recovers it.)
_UNPARSEABLE = "Action: do_the_thing\nAction Input: I have no idea\n"


def test_loop_breaker_aborts_on_fifth_consecutive_failure() -> None:
    """4 unparseable responses → still correctable (OutputParserException);
    the 5th aborts the whole agent run (RuntimeError, which AgentExecutor
    cannot swallow) so the chat shows a message instead of a 7-hour hang."""
    p, cb = _parser()
    for i in range(4):
        try:
            p.parse(_UNPARSEABLE)
            assert False, "expected OutputParserException"
        except OutputParserException:
            pass
        assert cb._consecutive_parse_failures == i + 1
    try:
        p.parse(_UNPARSEABLE)
        assert False, "expected RuntimeError on 5th consecutive failure"
    except RuntimeError as e:
        assert "could not interpret" in str(e)
    assert cb._consecutive_parse_failures == 5


def test_loop_breaker_reset_by_successful_tool_call() -> None:
    """A real tool execution resets the budget: the counter is per-format-loop,
    not per-run, so an agent that queries successfully may still fail (and be
    corrected) later."""
    from webui.backends.local import _EventCallback

    q: queue.Queue = queue.Queue()
    cb = _EventCallback(q)
    p = _RobustReActParser(cb)
    for _ in range(4):
        try:
            p.parse(_UNPARSEABLE)
        except OutputParserException:
            pass
    assert cb._consecutive_parse_failures == 4
    # Real tool start (not the synthetic _Exception correction tool).
    cb.on_tool_start({"name": "run_query"}, '{"sql": "SELECT 1"}')
    assert cb._consecutive_parse_failures == 0
    # A fresh budget afterwards: 4 more failures are fine, the 5th aborts.
    for _ in range(4):
        try:
            p.parse(_UNPARSEABLE)
        except OutputParserException:
            pass
    try:
        p.parse(_UNPARSEABLE)
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_loop_breaker_ignores_non_parser_errors() -> None:
    """Genuine errors (DB down, LLM unreachable) keep their normal path — only
    OutputParserException counts toward the abort budget."""
    p, cb = _parser()
    p._record_parse_failure(ValueError("boom"))
    assert cb._consecutive_parse_failures == 0


def test_loop_breaker_counts_strikes_from_other_paths() -> None:
    """The wrapper records failures from every raise site, not just the final
    give-up: the no-tool-call strike (strike 1) counts too."""
    p, cb = _parser()
    try:
        # Non-conversational prose with no tool call → strike 1.
        p.parse("The answer is somewhere in the database, I think.")
        assert False, "expected OutputParserException (no tool call)"
    except OutputParserException:
        pass
    assert cb._consecutive_parse_failures == 1
    try:
        p.parse(_UNPARSEABLE)
        assert False, "expected OutputParserException"
    except OutputParserException:
        pass
    assert cb._consecutive_parse_failures == 2


def test_escape_template_braces_doubles_literal_braces() -> None:
    """The MCP system content is embedded verbatim into a ChatPromptTemplate.
    Fix C added a JSON example {"sql": "..."} to CHAT_INSTRUCTIONS, which the
    template parser then read as a missing variable and broke agent
    construction. _escape_template_braces must double every literal brace so
    the JSON survives as literal text in the rendered prompt."""
    from langchain_core.prompts import ChatPromptTemplate

    raw = 'end with `Action: run_query` and `Action Input: {"sql": "..."}` and wait'
    escaped = _escape_template_braces(raw)
    assert "{{\"sql\": \"...\"}}" in escaped
    # No unescaped single braces may remain.
    i = 0
    while i < len(escaped):
        if escaped[i] == "{":
            assert escaped[i + 1] == "{", "unescaped single brace found"
            i += 2
        else:
            i += 1

    # Round-trip: build a real ChatPromptTemplate with the escaped content and
    # the ReAct placeholders; it must expose ONLY the real variables and must
    # render the JSON example back through intact.
    combined = escaped + "\nAction Input: {{\"sql\": \"SELECT ...\"}}\n{input}"
    t = ChatPromptTemplate.from_messages([("system", combined)])
    assert set(t.input_variables) == {"input"}
    rendered = t.format(input="hi")
    assert '{"sql": "..."}' in rendered
    assert '{"sql": "SELECT ..."}' in rendered


def _sim_on_tool_start(cb: _StubCallback, sql: str) -> None:
    """Replicates the exact state update _EventCallback.on_tool_start performs
    for the repetition guard, so the tests exercise the real decision logic."""
    from webui.backends.local import _clean_sql_input

    key = ("run_query", _clean_sql_input(sql))
    if key == cb._last_action_key:
        cb._consecutive_repeats += 1
    else:
        cb._last_action_key = key
        cb._consecutive_repeats = 1


def test_repetition_guard_aborts_on_third_identical_query() -> None:
    """granite4.2 at temp 0.1 re-issued the exact same COUNT query forever.
    Every response parses and executes, so only this guard can stop it.

    Real order per iteration: the executor calls parser.parse() FIRST
    (which runs _check_repetition against the repeat count from *previous*
    tool starts), then the tool runs and on_tool_start updates the counter.
    So a query may execute twice; the third parse aborts before it runs."""
    p, cb = _parser()
    sql = 'SELECT COUNT(*) AS count FROM feature WHERE objectclass_id = 1600'
    text = f"Action: run_query\nAction Input: {sql}\n"

    # Execution 1: no prior key -> allowed.
    assert p.parse(text) is not None
    _sim_on_tool_start(cb, sql)  # repeats = 1

    # Execution 2: repeats==1 (>=2 is False) -> still allowed.
    assert p.parse(text) is not None
    _sim_on_tool_start(cb, sql)  # repeats = 2

    # Execution 3: repeats==2 (>=2 is True) -> guard trips before it runs.
    try:
        p.parse(text)
    except RuntimeError as e:
        assert "re-running the exact same query" in str(e)
    else:
        raise AssertionError("expected RuntimeError on 3rd identical query")


def test_repetition_guard_resets_on_different_query() -> None:
    """A different query in between must reset the consecutive-identical
    counter, so legitimate repeated-but-alternating queries are not aborted.
    Same real order as above: parse (check) then on_tool_start (update)."""
    p, cb = _parser()
    a = "SELECT COUNT(*) AS count FROM feature WHERE objectclass_id = 1600"
    b = "SELECT COUNT(*) AS count FROM feature WHERE objectclass_id = 901"
    ta = f"Action: run_query\nAction Input: {a}\n"
    tb = f"Action: run_query\nAction Input: {b}\n"

    p.parse(ta)
    _sim_on_tool_start(cb, a)  # repeats = 1
    p.parse(ta)
    _sim_on_tool_start(cb, a)  # repeats = 2
    # Switch to a different query -> resets the run.
    p.parse(tb)
    _sim_on_tool_start(cb, b)
    assert cb._consecutive_repeats == 1
    # Back to a again is also fine (different from the last one).
    p.parse(ta)
    _sim_on_tool_start(cb, a)
    assert cb._consecutive_repeats == 1


def test_repetition_guard_ignores_final_answers() -> None:
    """The guard only inspects AgentActions. A Final Answer must never be
    treated as a repeated query."""
    p, cb = _parser()
    sql = "SELECT COUNT(*) AS count FROM feature WHERE objectclass_id = 1600"
    _sim_on_tool_start(cb, sql)
    p.parse(f"Action: run_query\nAction Input: {sql}\n")
    cb._tools_called = 1
    result = p.parse("Final Answer: There are 0 street lamps.")
    assert isinstance(result, AgentFinish)


_OLD_Q = "SELECT DISTINCT classname FROM objectclass WHERE is_toplevel = 1"
_NEW_Q = "SELECT id, classname, namespace_id FROM objectclass WHERE classname = 'CityFurniture'"


def test_keeps_last_turn_when_history_echoed() -> None:
    """A degenerate model (granite4.2 @ temp 0.1) re-emits the whole
    scratchpad in its response, so the fresh output contains MULTIPLE
    `Action Input:` lines. re.search() returns the FIRST match, which would
    execute the OLDEST echoed query and silently drop the model's genuinely
    new one. _keep_last_turn must trim to the newest action so the parser
    extracts the NEW query."""
    p, cb = _parser(tools_called=1)
    text = (
        "Thought: List the top-level objectclasses.\n"
        "Action: run_query\n"
        f'Action Input: {{"sql": "{_OLD_Q}"}}\n'
        "Observation: Building\nBridge\nCityFurniture\nWaterBody\n"
        "Thought: CityFurniture looks right, inspect it.\n"
        "Action: run_query\n"
        f'Action Input: {{"sql": "{_NEW_Q}"}}\n'
    )
    result = p.parse(text)
    assert isinstance(result, AgentAction)
    assert result.tool == "run_query"
    sql = _exec_sql(result)
    assert sql == _NEW_Q, f"expected NEW query, got: {sql!r}"


def test_keep_last_turn_is_noop_for_single_turn() -> None:
    """Normal output (a single Action Input) must be parsed exactly as before
    — the trim must not disturb healthy single-turn responses."""
    p, cb = _parser(tools_called=1)
    text = (
        "Thought: I now know the count.\n"
        "Action: run_query\n"
        f'Action Input: {{"sql": "{_NEW_Q}"}}\n'
    )
    result = p.parse(text)
    assert isinstance(result, AgentAction)
    assert _exec_sql(result) == _NEW_Q


def main() -> None:
    tests = [
        test_granite_sentence_as_tool_name_sql_json,
        test_unknown_tool_bare_sql_coerced,
        test_unknown_tool_nonsensical_input_raises,
        test_real_tool_name_passes_through_unchanged,
        test_custom_tool_names_respected,
        test_final_answer_unaffected,
        test_dict_tool_input_serialised,
        test_action_json_no_input_line_recovered,
        test_action_json_trailing_backtick_recovered,
        test_action_json_multiline_not_recovered,
        test_action_json_non_sql_raises,
        test_action_input_line_keeps_priority,
        test_loop_breaker_aborts_on_fifth_consecutive_failure,
        test_loop_breaker_reset_by_successful_tool_call,
        test_loop_breaker_ignores_non_parser_errors,
        test_loop_breaker_counts_strikes_from_other_paths,
        test_escape_template_braces_doubles_literal_braces,
        test_repetition_guard_aborts_on_third_identical_query,
        test_repetition_guard_resets_on_different_query,
        test_repetition_guard_ignores_final_answers,
        test_keeps_last_turn_when_history_echoed,
        test_keep_last_turn_is_noop_for_single_turn,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e!r}")
    print("=" * 50)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
