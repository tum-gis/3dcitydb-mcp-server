# Local model tool-calling probes — findings and fixes

This document records the empirical probing of local (Ollama) models against the
Fullstack Docker Web UI, the failure modes that were identified, the root causes in
code, and how each is (or will be) addressed.

## Scope and method

- **Environment:** Fullstack Docker deployment (`docker-compose.fullstack.yml`,
  image `citydb-mcp-agent:local`), Web UI at `http://localhost:7860`, Ollama
  server 0.33.2 on the LAN.
- **Code path under test:** the Web UI local backend
  (`production/webui/backends/local.py`) — a **text-format ReAct agent**
  (`create_react_agent` + `_RobustReActParser` around a single `run_query`
  SQL tool). The native tool-calling path (`src/citydb_mcp/agent.py`) is **not**
  exercised by the Web UI and is out of scope.
- **Probe query:** `What are the building volumes in Röblingweg?`
  Ground truth: **13 buildings**, e.g. `DEBY_LOD2_4965683 → 2438.905 m³`,
  `DEBY_LOD2_4965802 → 1293.152 m³`.
- **Fixed settings for all probes:** provider `ollama`, temperature `0.1`,
  thinking **off** (not all models support "thinking"; the `/no_think` marker is
  appended by the backend), prompt mode `full (auto)`, context **64K**
  (65,536), include-reasoning on, lessons off. Every probe started a **new
  conversation** first.

## Results (10 models probed)

| Model | Class | Behavior |
|---|---|---|
| `qwen3.8:27b` | ✅ works | 13 buildings, correct volumes |
| `qwen3.6:35b-a3b-q4_K_M` | ✅ works | Clean run: 13 buildings, correct volumes |
| `ministral-3:14b` | ✅ works | 13 buildings, correct volumes |
| `nemotron-3-nano:latest` | ✅ works | Full-precision volumes matching ground truth |
| `phi4:14b-q4_K_M` | ✅ works | Clean `Thought → Action: run_query → Observation` trace, 13 buildings |
| `gemma4:12b-it-q8_0` | ⚠️ wrong-sql | Valid ReAct and correct tool name, but the SQL used `CG_Volume(g.geometry)` without wrapping the geometry in `CG_MakeSolid(...)` → every volume came back `0.0` (the all-null/all-zero WARNING block fired) |
| `gemma4:26b-a4b-it-q4_K_M` | ❌ thinking-then-empty | Completes a full (and correct) query plan in the reasoning stream, then stops with **zero content** (`done_reason: stop`) — reproduced at the raw Ollama API level, see below |
| `gemma4:31b-it-q4_K_M` | ❌ thinking-then-empty | Same signature: full reasoning plan, empty `content`, clean stop |
| `granite4.2:30b-q4_K_M` | ✅ works (post-fix) | Three compounding defects, all addressed in code: (1) sentence-as-tool-name (auto-coerced to `run_query`); (2) **repetition loop** — re-emits the exact same `Thought`+`Action` forever (now caught by a repetition guard); (3) **echoed-scratchpad first-match** — re-emits the whole scratchpad in a fresh turn, so the parser used to execute the *oldest* echoed query instead of the new one (now trimmed by `_keep_last_turn`). After all fixes it answers `How many street lamps…` → **0** (CityFurniture objectclass exists, 0 features) in ~4 LLM calls |
| `gpt-oss:20b` | ⚠️ wrong-sql (post-fix) | Reasoning dump leaked as the answer **until the step-b parser fix** (recovery of native `tool_calls` / inline SQL from non-empty content — see below). Post-fix the tool runs reliably, but the model deterministically (2/2 live probes) picks the wrong metric: `child.val_double` (a child property value column, ≈ 12 m³) instead of `CG_Volume(CG_MakeSolid(g.geometry))` (≈ 2438 m³) and stops after one successful-but-wrong iteration — **verify the numbers**. |

Note: an earlier truncation failure of `qwen3.6:35b-a3b-q4_K_M` was
**non-reproducible** — it occurred during a window when the agent container was in
a broken state. The clean re-probe after force-recreating the container passed
fully, so it is treated as a container artifact, not a model failure mode.

## Root causes in code

### 1. Sentence-as-tool-name (granite4.2) — the standard-parser hole

`_RobustReActParser.parse()` calls `super().parse(clean)` **first** — i.e.
langchain's stock `ReActSingleInputOutputParser`
(`langchain/agents/output_parsers/react_single_input.py`). That stock parser
validates **syntax only** (`Action:` line present, `Action Input:` present). It
does **not** check the tool name against the registered tools. So granite4.2's

```
Action: I need to query the database for the building volumes
Action Input: {"sql": "SELECT ..."}
```

is a *syntactically valid* ReAct output and is returned as an `AgentAction` with
`tool="I need to query the database for the building volumes"`. The robust
parser's own tool-name inference/coercion logic (which would have mapped this to
`run_query`) is placed **after** the `super().parse()` call and therefore never
runs. The `AgentExecutor` then rejects the unknown tool and the conversation
degrades into retries until the iteration budget is exhausted.

### 2. Reasoning leak / no Action line (gpt-oss) — FIXED (step b)

`parse()` first strips `reasoning`/`thinking` tags; when the stripped text is
empty it falls back to the raw reasoning block, and when there is simply no
`Action:`/`Final Answer:` line it reaches the **prose fallback**, which treats
the free text as a final answer. A model that streams its internal reasoning as
ordinary text (gpt-oss) therefore produced a confident-looking answer built from
leaked reasoning, without ever calling `run_query`. The `_require_tool_call`
guard raises on the first no-tool answer, but on the second violation it lets the
answer through deliberately (to avoid an infinite loop) — so the leak reached the
user.

**Wire-verified root cause (2026-09-03, Ollama 0.33.2, streaming proxy capture):**
gpt-oss:20b streams its reasoning into the **`content` field** (non-empty) for
the real ~35 k-char production system prompt, and only into the `thinking` field
for small/harness prompts. In *both* modes it emits a native Ollama
`tool_calls` chunk in the last streamed delta before `done: true`
(`function.name = "run_query"`, correct SQL arguments). langchain-ollama 0.3.10
maps that into `AIMessage.tool_calls` but omits the `think` field when
`reasoning` is `None`. The pre-fix recovery hook in `_RobustReActParser` only
fired when content was **empty** — so with the production prompt (reasoning in
content) it never triggered, the stock ReAct parser saw prose, the two-strike
guard let the prose through, and 4/4 live UI probes returned the reasoning dump
as the "final answer".

**The fix** (in `webui/backends/local.py`, `parse_result`): before falling back
to `super().parse_result`, when the result is a single `AIMessage`
(1) check the message's native `tool_calls` for a `run_query` call
(`_action_from_tool_calls`, with argument normalisation and
`_clean_sql_input`), and (2) if no tool_calls, extract an inline
`SELECT`/`WITH` statement from the content (`_action_from_embedded_sql`, a
quote-aware procedural scanner — identifier dots like `g.geometry` never
terminate the statement; only an unanchored mid-sentence `SELECT` mention is
skipped). Two guards keep the recovery from hijacking genuine ReAct output: it
only applies while **no tool has run yet** (`_tools_called == 0`) and the
content contains **no `Action:` / `Final Answer:` line**.

**Post-fix evidence:** 15/15 unit tests in `tests/test_local_gptoss.py`
(including the real wire shape: reasoning-in-content + trailing tool_call).
Two live UI probes after deployment both logged
`[local] parser: recovered run_query from Ollama message (content_chars=234
tool_calls=1)` → `run_query` executed → a 13-row table was rendered (no
reasoning dump, no `OUTPUT_PARSING_FAILURE`).

**Residual model-quality issue (not a parser bug):** the recovered
iteration-1 tool_call deterministically (2/2 probes) selected
`child.val_double AS volume_m3` (a child property value column — values ≈
11.9–14.2) instead of `CG_Volume(CG_MakeSolid(g.geometry))` (ground truth
2438.905 m³ for `DEBY_LOD2_4965683`). Because the wrong query *succeeds* with
13 plausible rows, the ReAct loop has no failure trigger and the model answers
from the wrong results without a second iteration. Earlier wire captures show
iteration 2 would have carried the correct `CG_Volume` SQL — but only if the
first query had failed or returned empty. This is why `gpt-oss` is
recommended with a **verify-the-metric** warning and `full` prompt mode, not
recommended-unqualified. A stochastic refusal mode ("I'm sorry, but I can't
help…") also exists at low but non-zero frequency.

### 3. Thinking-then-empty responses (gemma4:26b, gemma4:31b) — known Ollama/gemma4 bug

These completions arrive with **empty `content` even though the model did
substantial work**: the entire output (a complete, correct query plan — right
joins, right filters, `CG_Volume(CG_MakeSolid(...))`) is emitted into the
reasoning/thinking stream, and then generation stops at the thinking→content
transition with `done_reason: "stop"`. Not a timeout, not a token budget
(10,352 prompt tokens, 500–1,300 output tokens of a 65,536 context).

**Root cause isolation (2026-08-31, Ollama 0.33.2):** the failure was
reproduced against the **raw Ollama `/api/chat` endpoint** with the exact
35,452-char system prompt extracted from the running container, for `think`
= false / omitted / true, with and without a `num_predict` cap — always
empty. A synthetic filler prompt of the *same or larger size* produces valid
ReAct output, so the trigger is the interaction of this prompt's content with
gemma4's thinking mode, **not** the Web UI, the container, or any recent code
change (`git status` confirmed no webui code changes).

This matches a documented bug family of the gemma4 generation on Ollama:

- [ollama/ollama#15502](https://github.com/ollama/ollama/issues/15502) — gemma4:31b/26b
degeneration (repetition collapse / low-entropy traps) in the Ollama
sampling path; cross-runtime tests show the same GGUFs run clean on
llama.cpp, i.e. the bug is in the Ollama runner, not the weights.
- [google-deepmind/gemma#622](https://github.com/google-deepmind/gemma/issues/622) —
companion model-level report of the token-repetition tendency.
- [ollama/ollama#15428](https://github.com/ollama/ollama/issues/15428) — gemma4:26b
empty responses on long system prompts (earlier iteration; the gemma4
parser/renderer dropped non-thinking output on some 0.20.x releases).
- [ollama/ollama#15260](https://github.com/ollama/ollama/issues/15260) / [#15386](https://github.com/ollama/ollama/issues/15386) —
gemma4 thinking-mode transitions breaking/ignoring structured output.

The Web UI behavior (empty-output guard → graceful `AgentFinish` "The model
did not produce a response after retrying…") is the intended reaction to
this model-side failure.

**Practical consequence:** these two models are *planning correctly but
cannot deliver* on this stack. Until Ollama fixes the gemma4 thinking
transition, they are not usable for the ReAct workflow; option c/e should
mark them as not recommended (see below).

### 4. Wrong SQL, valid ReAct (gemma4:12b)

The agent protocol worked perfectly; the model simply generated
`CG_Volume(g.geometry)` on a surface geometry without the
`CG_MakeSolid(g.geometry)` wrapper that 3DCityDB v5 requires, producing all-zero
volumes. This is a prompt/grounding problem, not a parsing problem — the
`_is_all_null_result` WARNING (with the `CG_MakeSolid` hint) in the observation
is the existing mitigation.

### 5. What is deliberately *not* changed

The loop-detection behavior in `_EventCallback.on_tool_end` — the
`Retrying… (attempt N/MAX_ITERATIONS)` status emitted when a tool result is an
error or returns 0 rows — together with `_require_tool_call`'s two-strike
retry semantics, is a hard requirement and **must not be modified**. All fixes
below work around the failure modes without touching that machinery.

### 6. Repetition loop (granite4.2) — the guard the parse-breaker could not see

granite4.2 has a second, independent failure mode beyond the sentence-as-tool
name: it **degenerates into an exact repetition loop**, re-emitting the *identical*
`Thought: … Action: run_query … Action Input: {…}` block over and over, and
stopping producing anything new. Unlike the parse-failure mode, **every
re-emitted block parses and executes cleanly** — the model really runs the same
SQL, gets the same observation, and outputs the same next step. The
`_MAX_CONSECUTIVE_PARSE_FAILURES` breaker (5 unparseable responses in a row) is
blind to this because nothing is failing.

**The fix:** a **repetition guard** tracked in the callback.
`_EventCallback.on_tool_start` records a canonical key of the action
(`_action_key` → tool name + normalized SQL) and increments
`_consecutive_repeats` when the key matches `_last_action_key`. When
`_consecutive_repeats >= _MAX_CONSECUTIVE_IDENTICAL` (2) and the *next*
identical action is parsed, `_RobustReActParser.parse` (via
`_check_repetition`) raises an `OutputParserException` that aborts the agent
loop with a clean `Agent error: The model kept re-running the exact same query
without making progress…` instead of burning the whole `max_iterations=10`
budget (and, in the earlier unbounded form, an effectively infinite loop).

**Ordering note:** the executor calls `parser.parse()` *before* each tool runs,
so the guard sees the state recorded by *previous* tool starts. Net effect: the
same query may execute **twice**; the third identical parse aborts.

**Why the raise is in the parser, not the callback:** LangChain's
`CallbackManager.handle_event` **swallows handler exceptions** unless
`raise_error=True` is passed, and the executor does not set it for tool events.
Raising from a callback would be silently dropped. Raising from `parse()` is
inline in the executor's own call stack, so the exception propagates and trips
the executor's error handling.

### 7. Echoed-scratchpad first-match (granite4.2) — the "new query is ignored" bug

The repetition loop is a *symptom*; the deeper bug made it worse and could
silently drop genuinely new work. When the model re-emits the whole scratchpad
in a fresh turn, its output contains **multiple** `Action Input:` lines — one
per echoed step plus the new one. The stock ReAct parser (and the robust
fallback) extract the action with `re.search()`, which returns the **first**
match — i.e. the **oldest echoed query**, not the new one. The new query the
model actually intended was silently discarded and the old query re-executed.

**Deterministic proof (fake-LLM probe, no model in the loop):**
- clean single-turn output (`Action Input: {NEW}` only) → parser extracts
  **NEW** ✔;
- echoed-history output (`Action Input: {OLD}` … `Observation:` …
  `Action Input: {NEW}`) → parser extracted **OLD** (the bug);
- after `_keep_last_turn` → extracts **NEW** in all cases ✔.

**The fix — `_keep_last_turn`:** before parsing, if the cleaned text contains
**two or more** `Action Input` markers, trim everything before the **last**
marker (keeping the model's newest turn). A `Action:`-only variant is handled
the same way when no `Action Input` line exists. For normal single-turn output
(the common case for healthy models) the method is a **no-op**, so it cannot
false-positive. This is what made the live re-verification pass: call 3 of the
street-lamp run extracted and executed the *new* `CityFurniture` COUNT query
instead of re-running the oldest echoed `is_toplevel` query.

### 8. The within-chain chat log is **not** the bug (verified, disproved hypothesis)

A plausible suspicion is that the ReAct agent accumulates a new chat message
per step (so by iteration N the prompt carries N copies of the running
scratchpad, making repetition likely). This is **false** for this code path and
was verified two ways:

- **Fake-LLM probe** driving the real `create_react_agent` + `AgentExecutor`
  with a scripted `FakeMessagesListChatModel`: **every LLM call receives exactly
  3 messages** — `system`, `human` (the user question), and **one** `assistant`
  message holding the growing scratchpad (`agent_scratchpad`), which grows in
  place (8 → 213 → 416 chars across three iterations). There is **no**
  per-step message accumulation.
- **Live diagnostic** (`[local] llm_start` from `on_chat_model_start`): the
  street-lamp run logged `call=N msgs=3 prompt_chars=… breakdown=[system:36865,
  human:321, ai:K]` with the `ai` (scratchpad) field growing
  (8 → 1052 → 3100 → 4116) and `msgs` fixed at 3 every call.

The ReAct prompt is
`[("system", …), MessagesPlaceholder("chat_history"), ("human", "{input}"),
("assistant", "Thought:{agent_scratchpad}")]`; within one chain
`chat_history` is empty and the scratchpad is a single growing assistant
message. The **"Include all reasoning steps in context (Ollama)"** toggle
(`reasoning_replay` in `app.py`) affects **only cross-query history** — it wraps
prior *turns'* final answers as `[Reasoning]\n{trace}\n\n[Answer]\n{answer}` in
`chat_history` between user turns, never within a single ReAct chain. So the
"verbatim repeated thoughts" seen in the Agent activity panel were granite4.2
re-emitting its own scratchpad in fresh output (root causes §6/§7), not a
plumbing bug.

### 9. Template braces in the system prompt (granite4.2)

granite4.2 (and the ReAct prompt template itself) chokes on **literal `{`/`}`**
in the system prompt, because the prompt is rendered through a
`ChatPromptTemplate` that treats `{…}` as format fields. The 35k-char 3DCityDB
schema prose contains many such braces. `_escape_template_braces`
double-escapes them before rendering so they survive as literal text. This was
one of the preconditions for granite4.2 to emit *parseable* ReAct at all.

## Addressing the failure modes

### a) Validate/canonicalize the tool name in the parser (fixes granite4.2) — DONE

Implemented in `webui/backends/local.py`. `_RobustReActParser.__init__` takes
`tool_names: set[str]` (passed in from `react_stream` as `tool_names={sql_tool.name}`);
`_coerce_unknown_tool(text, log)` runs the coercion (input parses as JSON with
an `"sql"` key, or starts with `SELECT`/`WITH`/`INSERT`/`UPDATE`/`DELETE`, or
the raw text contains a `"sql"` key) and `parse()` applies it when the parsed
action's `tool` is not in `tool_names`, logging
`[local] parser: coerced unknown tool '<sentence>' -> run_query`. Non-coercible
unknown tools still raise so the existing retry machinery takes over. The
`Action: {json}` recovery path uses the same coercion. Loop detection is
untouched.

**Test:** `tests/test_local_parser.py` — 22 tests covering: granite's
sentence-as-tool-name + sql JSON → coerced `AgentAction("run_query")`;
`Action: {json}` recovery; normal `Action: run_query` unchanged; `Final Answer:`
→ `AgentFinish`; no-Action prose → strike 1 raises, strike 2 answers;
degenerate `Final Answer: 1` → degenerate-final guard; empty output → strike 1
raises, strike 2 → graceful `AgentFinish`; **and the §7 first-match regression
tests** (clean single-turn → NEW extracted; echoed history → NEW extracted after
`_keep_last_turn`, OLD before).
**Acceptance (met):** fresh browser probe of `granite4.2:30b-q4_K_M`, street
lamps → correct `0` in ~4 LLM calls (see §7 live evidence); building volumes
(13) was the original acceptance query in the pre-fix probing rounds.

### b) Recover native tool calls / inline SQL from reasoning content (fixes gpt-oss) — DONE

Implemented in `webui/backends/local.py` as a `parse_result` override on
`_RobustReActParser` (full analysis and evidence under "Root cause §2").
Instead of switching gpt-oss to a native tool-calling pipeline, the recovery
happens inside the existing ReAct pipeline: when the LLM result is a single
`AIMessage` whose content is non-empty prose (the gpt-oss reasoning-dump case),
the parser first checks `msg.tool_calls` for a `run_query` call and, failing
that, scans the content for an inline `SELECT`/`WITH` statement. Guards:
`_tools_called == 0` (never hijacks a later iteration) and no
`Action:`/`Final Answer:` line in the content (genuine ReAct output is left to
`super().parse_result`).

**Test:** `tests/test_local_gptoss.py` — 15 tests covering the real wire shape
(reasoning-in-content + trailing `tool_calls` chunk), inline SQL with
identifier dots, unanchored mid-sentence `SELECT` mentions (ignored),
recovery skipped after a tool already ran, and the unchanged two-strike
behaviour for plain prose. **Acceptance:** two live UI probes after
deployment logged `recovered run_query from Ollama message` and rendered a
13-row table (residual wrong-metric SQL is a model-quality issue, see §2).

### c) Per-model LLM configuration — DONE

Implemented in `webui/backends/local.py` as a prefix-keyed registry with
`temperature`, `enable_thinking`, and `num_ctx` defaults. `_resolve_llm_kwargs`
applies per-model defaults only where the user left the corresponding control
unchanged, and explicit user values always win. **Test:**
`tests/test_local_kwargs.py` — 9 tests.

The Web UI's **Set temperature** checkbox is off by default. While it is off,
the temperature field is omitted from both cloud/OpenAI-compatible and Ollama
requests, allowing the provider or model default to apply. When enabled, the
slider value is sent explicitly.

### h) OpenAI-compatible reasoning effort — DONE

The existing Thinking dropdown is forwarded for `provider == "openai"` through
LiteLLM as `reasoning_effort`: `off` → `none`, and `low`/`medium`/`high` map
directly. `temperature` remains unchanged. Anthropic and Ollama requests keep
their existing provider-specific behavior. If an OpenAI-compatible endpoint
rejects the field as unsupported, the request retries once without it and the
model/base-URL pair is remembered for later calls.

**Test:** `tests/test_openai_kwargs.py` — 5 tests covering mapping, provider
isolation, omission, rejection detection, and fallback retry. Support remains
endpoint/model-dependent; the fallback preserves compatibility but cannot add
reasoning to a server that does not implement it.

### e) Model-class routing registry — DONE

Implemented in `production/webui/model_profiles.py`: the five empirical classes
(`works` / `wrong-sql` / `thinking-then-empty` / `sentence-as-tool` /
`reasoning-leak`) mapped from model-name prefixes, with per-class handling:

- `works` → no warning, recommended;
- `sentence-as-tool` → recommended *after fix (a)*, informational note that
  tool-name misuse is auto-repaired;
- `wrong-sql` → default prompt mode `full` (stronger schema emphasis);
- `thinking-then-empty` (gemma4:26b/31b) → UI warning "model completes
  reasoning but delivers an empty answer on this Ollama version (known
gemma4 bug)", not recommended;
- `reasoning-leak` → UI warning "may leak reasoning as the answer", not
  recommended;
- unknown → class `unknown`, no warning.

`app.py` surfaces the per-model warning in the existing warning slot next to the
model dropdown and uses the class's default when the user leaves prompt mode on
"auto". **Test:** `tests/test_model_profiles.py` — 4 tests (family-prefix
classification to the expected class, per-class warning/recommendation flags,
unknown models map to `unknown`).

### f) Repetition guard (fixes granite4.2 §6) — DONE

Full design, ordering, and the callback-exception-swallowing discovery under
"Root cause §6". In short: `_EventCallback.on_tool_start` tracks a canonical
action key (tool name + `_clean_sql_input`-normalized SQL) and a
`_consecutive_repeats` counter; `_RobustReActParser._check_repetition` raises
from `parse()` when the same action is about to run a third time in a row,
aborting with a clean `Agent error` instead of burning `max_iterations`.

**Test:** covered in `tests/test_local_parser.py` (repetition-guard cases).

### g) Echoed-scratchpad first-match fix (fixes granite4.2 §7) — DONE

Full analysis and deterministic fake-LLM proof under "Root cause §7".
`_keep_last_turn` trims the parsed text to the last `Action Input`/`Action`
marker when two or more exist so `re.search()` extracts the model's *new*
query instead of the oldest echoed one. No-op for single-turn output.

**Test:** `tests/test_local_parser.py` — `test_keeps_last_turn_when_history_echoed`,
`test_keep_last_turn_is_noop_for_single_turn`.
**Live evidence:** call 3 of the street-lamp run executed the new
`SELECT COUNT(*) … WHERE objectclass_id = 1600` query (correct answer: 0).

### Considered and not implemented

- **Changing loop detection** — rejected: the 10-attempt
  `Retrying… (attempt N/MAX_ITERATIONS)` behavior is required and stays as is.
- **Ollama upgrade** — noted as future work; not part of this change set.

## How changes are tested/deployed in this deployment

- The Dockerfile (`production/docker/Dockerfile`) **copies** `production/webui/`
  into the image (`/app/webui/`) — there is **no bind mount** for webui code.
  After any webui change: `docker compose -f docker-compose.fullstack.yml build
  citydb-agent`, then `up -d --force-recreate citydb-agent`, then reload the
  browser page.
- Unit tests are plain `python <file>.py` scripts (no `pytest` module in the
  image). Two ways to run them inside the image (which has langchain):
  - ad-hoc, no rebuild: `docker cp <repo>/tests
    production-citydb-agent-1:/tmp/tb_tests` (from cwd `production/`, use
    `docker cp ..\tests production-citydb-agent-1:/tmp/tb_tests` on
    Windows/PowerShell), then
    `docker exec production-citydb-agent-1 python /tmp/tb_tests/<file>.py`.
    **Note:** a `--force-recreate` wipes `/tmp/tb_tests`; re-stage after every
    recreate.
  - from a fresh container: `docker run --rm -v <repo>:/repo
    citydb-mcp-agent:local python /repo/tests/<file>.py`.
  - Current totals (all green): `test_local_parser.py` 22/22,
    `test_local_gptoss.py` 15/15, `test_local_kwargs.py` 8/8,
    `test_model_profiles.py` 4/4, `test_openai_kwargs.py` 5/5.
- Authoritative parse diagnostics come from `docker logs
  production-citydb-agent-1` — key lines: `[local] llm_start call=N msgs=…
  breakdown=[role:chars, …]` (per-LLM-call prompt shape), `[local] parser:
  coerced unknown tool …`, `Could not parse LLM output: …`, and the repetition
  abort `The model kept re-running the exact same query without making
  progress…`.
