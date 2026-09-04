"""Unit tests for the Ollama tool_calls recovery in _RobustReActParser.

gpt-oss:20b always answers with empty content and delivers the query through
the Ollama-native tool_calls array (see production/docs/local-model-probing.md).
langchain-core's default parse_result only forwards generation.text to parse(),
so _RobustReActParser overrides parse_result and recovers an AgentAction from
the AIMessage.tool_calls when the content is empty.

Run from the repo root (host):
    python tests/test_local_gptoss.py
or inside the agent image (has langchain):
    docker exec production-citydb-agent-1 python /tmp/tb/tests/test_local_gptoss.py
"""

import json
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

from langchain_core.agents import AgentAction, AgentFinish  # noqa: E402
from langchain_core.exceptions import OutputParserException  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration  # noqa: E402

from webui.backends.local import _RobustReActParser  # noqa: E402


class _StubCallback:
    """Mimics the subset of _EventCallback the parser touches."""

    def __init__(self) -> None:
        self._tools_called = 0
        self._parse_errors = 0
        self._consecutive_parse_failures = 0
        self._last_action_key = None
        self._consecutive_repeats = 0


def _parser(tools_called=0) -> tuple[_RobustReActParser, _StubCallback]:
    cb = _StubCallback()
    cb._tools_called = tools_called
    return _RobustReActParser(cb, tool_names={"run_query"}), cb


def _gen(content: str, tool_calls: list | None = None) -> ChatGeneration:
    return ChatGeneration(
        message=AIMessage(content=content, tool_calls=tool_calls or [])
    )


SQL = "SELECT f.objectid, CG_Volume(g.geometry) AS vol FROM feature f WHERE f.objectclass_id = 901"


def test_recover_dict_args_sql_key() -> None:
    """The observed gpt-oss shape: empty content + dict args with 'sql'."""
    p, _ = _parser()
    result = p.parse_result(
        [_gen("", [{"name": "run_query", "args": {"sql": SQL}, "id": "call_1"}])]
    )
    assert isinstance(result, AgentAction)
    assert result.tool == "run_query"
    assert result.tool_input.startswith("SELECT")
    assert "objectclass_id = 901" in result.tool_input


def test_recover_json_string_args() -> None:
    """args may arrive as a JSON string (raw Ollama 'arguments' field)."""

    class _Msg:
        content = ""
        tool_calls = [
            {"name": "run_query", "args": json.dumps({"sql": SQL}), "id": "call_2"}
        ]

    p, _ = _parser()
    action = p._action_from_tool_calls(_Msg())
    assert action is not None
    assert action.tool == "run_query"
    assert action.tool_input.startswith("SELECT")


def test_recover_query_key() -> None:
    """The 'query' key is accepted like the text recovery paths do."""
    p, _ = _parser()
    result = p.parse_result(
        [_gen("", [{"name": "run_query", "args": {"query": SQL}, "id": "call_3"}])]
    )
    assert isinstance(result, AgentAction)
    assert result.tool_input.startswith("SELECT")


def test_recover_case_insensitive_tool_name() -> None:
    p, _ = _parser()
    result = p.parse_result(
        [_gen("", [{"name": "Run_Query", "args": {"sql": "SELECT 1"}, "id": "call_4"}])]
    )
    assert isinstance(result, AgentAction)
    assert result.tool == "run_query"


def test_non_sql_tool_call_falls_through() -> None:
    """Empty content + non-SQL tool_call → stock empty-output strike 1."""
    p, cb = _parser()
    try:
        p.parse_result(
            [_gen("", [{"name": "run_query", "args": {"sql": "I have no idea"}, "id": "call_5"}])]
        )
        assert False, "expected OutputParserException"
    except OutputParserException:
        pass
    assert cb._parse_errors == 1


def test_wrong_tool_name_ignored() -> None:
    """A tool_call with an unrelated name is not a recovery candidate."""
    p, cb = _parser()
    try:
        p.parse_result(
            [_gen("", [{"name": "think", "args": {"sql": SQL}, "id": "call_6"}])]
        )
        assert False, "expected OutputParserException"
    except OutputParserException:
        pass


def test_no_tool_calls_unchanged() -> None:
    """Empty content without tool_calls keeps the two-strike behaviour."""
    p, cb = _parser()
    try:
        p.parse_result([_gen("")])
        assert False, "expected OutputParserException"
    except OutputParserException:
        pass
    # Second strike → graceful give-up.
    result = p.parse_result([_gen("")])
    assert isinstance(result, AgentFinish)
    assert "did not produce a response" in result.return_values["output"]


def test_nonempty_content_ignores_tool_calls() -> None:
    """When real ReAct text is present, tool_calls must not hijack parsing."""
    p, _ = _parser()
    result = p.parse_result(
        [
            _gen(
                'Action: run_query\nAction Input: {"sql": "SELECT 42"}\n',
                [{"name": "run_query", "args": {"sql": SQL}, "id": "call_7"}],
            )
        ]
    )
    assert isinstance(result, AgentAction)
    assert "SELECT 42" in result.tool_input


def test_parse_with_string_input_unchanged() -> None:
    """Message + plain-string generations both bypass the recovery branch
    when they carry no (recoverable) tool_calls."""
    p, cb = _parser()
    cb._tools_called = 1
    result = p.parse_result([_gen('Action: run_query\nAction Input: SELECT 7\n')])
    assert isinstance(result, AgentAction)
    assert result.tool_input == "SELECT 7"
    # String generations bypass the message branch entirely.
    from langchain_core.outputs import Generation

    result2 = p.parse_result([Generation(text="Final Answer: done\n")])
    assert isinstance(result2, AgentFinish)
    assert result2.return_values["output"] == "done"


REASONING_DUMP = (
    "The user wants building volumes in Röblingweg. I need to find the street, "
    "then the buildings on it, then their volumes. In CityDB volumes are computed "
    "with CG_Volume over surface geometry. The address table links features to "
    "streets. So I should join feature, property_address and surface_geometry, "
    "filter by street name, and compute the solid volume. The query I need is:"
    "SELECT f.objectid, CG_Volume(CG_MakeSolid(g.geometry)) AS volume_m3 "
    "FROM feature f JOIN property p ON p.feature_id = f.featureid "
    "WHERE f.objectclass_id = 901;"
)


def test_recover_tool_calls_with_reasoning_content() -> None:
    """Probe-4 wire shape: reasoning dump in content + native tool_call.

    The old recovery hook required empty content and never fired, so the
    prose was returned as the final answer.  It must now recover the call.
    """
    p, _ = _parser()
    result = p.parse_result(
        [
            _gen(
                REASONING_DUMP,
                [{"name": "run_query", "args": {"sql": SQL}, "id": "call_w1"}],
            )
        ]
    )
    assert isinstance(result, AgentAction)
    assert result.tool == "run_query"
    assert result.tool_input.startswith("SELECT")
    assert "objectclass_id = 901" in result.tool_input


def test_recover_inline_sql_without_tool_call() -> None:
    """Reasoning dump with the SQL written inline in prose, no tool_call."""
    p, _ = _parser()
    result = p.parse_result([_gen(REASONING_DUMP)])
    assert isinstance(result, AgentAction)
    assert result.tool == "run_query"
    # Identifier dots (g.geometry / p.feature_id) must survive intact.
    assert "CG_MakeSolid(g.geometry)" in result.tool_input
    assert "p.feature_id = f.featureid" in result.tool_input
    assert result.tool_input.rstrip().endswith(";") or result.tool_input.strip().endswith("901")


def test_embedded_sql_identifier_dots_not_terminated() -> None:
    """g.geometry style dots must not be treated as sentence ends."""
    p, _ = _parser()
    raw = p._extract_inline_sql("query: SELECT a.x, b.y FROM a JOIN b ON a.id = b.id;")
    assert raw is not None
    assert raw.strip() == "SELECT a.x, b.y FROM a JOIN b ON a.id = b.id;"


def test_embedded_sql_unanchored_mention_ignored() -> None:
    """A SELECT mentioned mid-sentence (no line/colon anchor) is no query."""
    p, _ = _parser()
    text = (
        "I think we can solve this with SELECT statements in general, "
        "and maybe a JOIN or two depending on the schema."
    )
    assert p._action_from_embedded_sql(text) is None


def test_final_answer_after_tool_not_hijacked() -> None:
    """tools_called=1 + Final Answer prose mentioning SQL → AgentFinish.

    Guards against the recovery path coercing a legitimate post-tool
    final answer into another query.
    """
    p, _ = _parser(tools_called=1)
    result = p.parse_result(
        [
            _gen(
                "Final Answer: The building has a volume of 2438.9 m3. "
                "(For reference the SQL used was: SELECT 1)\n"
            )
        ]
    )
    assert isinstance(result, AgentFinish)
    assert "2438.9" in result.return_values["output"]


def test_plain_prose_no_sql_still_raises() -> None:
    """Reasoning dump with no SQL and no tool_call → strike 1 unchanged."""
    p, cb = _parser()
    try:
        p.parse_result([_gen("Let me think about this. I need the street first.")])
        assert False, "expected OutputParserException"
    except OutputParserException:
        pass
    assert cb._parse_errors == 1


def main() -> None:
    tests = [
        test_recover_dict_args_sql_key,
        test_recover_json_string_args,
        test_recover_query_key,
        test_recover_case_insensitive_tool_name,
        test_non_sql_tool_call_falls_through,
        test_wrong_tool_name_ignored,
        test_no_tool_calls_unchanged,
        test_nonempty_content_ignores_tool_calls,
        test_parse_with_string_input_unchanged,
        test_recover_tool_calls_with_reasoning_content,
        test_recover_inline_sql_without_tool_call,
        test_embedded_sql_identifier_dots_not_terminated,
        test_embedded_sql_unanchored_mention_ignored,
        test_final_answer_after_tool_not_hijacked,
        test_plain_prose_no_sql_still_raises,
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
