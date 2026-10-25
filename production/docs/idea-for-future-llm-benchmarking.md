# Idea: Reproducible LLM Benchmarking for the CityDB ChatBot

> **Status: benchmarking work has already started and is implemented.**
>
> The shared chat runtime, the interactive `3dcitydb-chat chat` CLI, the CSV
> model/question inputs, the `benchmark` subcommand, the incremental results
> CSV, and the standalone local grading GUI for the benchmark categories are
> already built into this repository. The current state, usage, CSV formats,
> and result/grading workflow are documented in
> [benchmarking.md](./benchmarking.md) — refer to that file rather than the
> proposals below, which remain useful background for the original design.

## Motivation

The ChatBot can query a 3DCityDB through OpenAI, Anthropic, and Ollama-backed
models. A repeatable benchmark runner would make it possible to compare these
models and interfaces on the same questions, data, and chatbot settings,
instead of relying on ad hoc manual trials.

The proposed tool reads a list of models and a set of questions with
ground-truth answers from CSV files. It runs every selected question for each
model, repeats the runs to measure answer variability, and writes detailed,
durable results for later comparison. An optional, separately configured LLM
grader can compare each generated answer to the reference answer.

The benchmark should live in this repository and reuse the production
ChatBot's runtime. A chatbot behavior or prompt improvement should therefore
be reflected in both the WebGUI and benchmark rather than in two independent
implementations.

## Goals

- Compare OpenAI, Anthropic, and Ollama chatbot configurations against the
  same question set.
- Allow OpenAI mode to target hosted or local OpenAI-compatible APIs by
  specifying a per-model base URL.
- Compare the same local model through the OpenAI-compatible interface and
  through the Ollama interface as distinct benchmark configurations.
- Repeat questions to measure correctness and response consistency.
- Support independent single-turn questions and ordered, multi-turn question
  groups.
- Preserve the original query, ground truth, model configuration, generated
  answer, run metadata, and optional evaluator result in the output.
- Run a set of questions against a user-selected CityGML dataset in the
  configured 3DCityDB instance.
- Keep answer generation and optional answer grading in distinct chat
  sessions, so the grader can be held constant across different answering
  models.

## Current ChatBot and Proposed Reuse

The production WebGUI already has the relevant provider implementations and
chat behavior:

- `production/webui/backends/cloud.py` implements native tool calling for
  OpenAI and Anthropic.
- `production/webui/backends/local.py` implements the Ollama ReAct path.
- `production/webui/backends/__init__.py` dispatches to the selected backend.
- `production/webui/app.py` currently coordinates conversation history,
  system prompt selection, context accounting and trimming, answer streaming,
  reasoning replay, and optional distilled summaries.
- `production/webui/mcp_client.py` calls the CityDB MCP server and its
  `run_query` tool.

The command-line tools should not create another agent loop. The
implementation should extract the Gradio-independent turn/conversation
orchestration into a shared runtime module. The WebGUI should call that module
after the refactor, and both the interactive CLI and benchmark CLI should call
the same module with the same provider, prompt, context, and reasoning
behavior. Gradio and the CLI should remain presentation layers.

The shared runtime should be responsible for the behavior that can affect
answers: constructing messages, selecting the prompt mode, estimating and
capping context, calling the backend, executing MCP tools, and collecting the
final answer and run metrics. User-interface rendering, progress bars, and
browser state remain the responsibility of the WebGUI.

## Input Files

### Model matrix

`models.csv` describes one answering-model configuration per row. Provider and
model are required. `base_url` is optional and is most useful with the
OpenAI-compatible backend. The same model may occur on multiple rows with
different provider/base URL settings.

```csv
provider,model,base_url,temperature,thinking_level,prompt_mode
openai,gpt-4.1,,,,
anthropic,claude-sonnet-4-5,,,,
openai,qwen3.8:27b,http://localhost:11434/v1,0,off,full
ollama,qwen3.8:27b,,,off,full
```

The final two rows illustrate a local model tested through both the
OpenAI-compatible API and Ollama's native interface. These are separate
configurations and should be distinguishable in the output.

Proposed columns:

| Column | Required | Description |
|--------|----------|-------------|
| `provider` | Yes | ChatBot provider/backend: `openai`, `anthropic`, or `ollama`. |
| `model` | Yes | Provider's model name. |
| `base_url` | No | OpenAI-compatible endpoint URL; blank means use the configured default. |
| `temperature` | No | Per-model override; blank inherits the CLI setting or ChatBot default. |
| `thinking_level` | No | Per-model override such as `off`, `low`, `medium`, or `high`, subject to provider support. |
| `prompt_mode` | No | Per-model override such as `auto`, `compact`, or `full`. |

CLI settings provide run-wide defaults. A populated CSV value overrides its
matching CLI default; a blank cell inherits it. The exact precedence should be
documented and validated. Credentials such as API keys must come from the
existing environment/.env configuration and must never be placed in the model
CSV or copied into benchmark results.

### Questions and ground truth

The question/reference structure is informed by
[IFC-Bench v2](https://huggingface.co/datasets/sylvainHellin/ifc-bench), which
uses fields including `id`, `question`, and `ground_truth`. The benchmark
should not reuse IFC-specific project/model identifiers: CityGML datasets are
identified by a user-supplied `citygml_dataset` value instead.

```csv
id,question,ground_truth,citygml_dataset,category,group_number,sequence_number
1,"How many buildings are in this dataset?","There are 42 buildings.",Munich_LOD2,1,,
2,"Which buildings are taller than 50 meters?","The City Hall and Tower A.",Munich_LOD2,2,17,1
3,"What are their building IDs?","The IDs are bldg_17 and bldg_29.",Munich_LOD2,2,17,2
4,"What is the total building count?","There are 42 buildings.",Harthof,1,,
```

Proposed columns:

| Column | Required | Description |
|--------|----------|-------------|
| `id` | Yes | Stable query identifier, unique within the query file. |
| `question` | Yes | Natural-language question presented to the ChatBot. |
| `ground_truth` | Yes | Reference answer, preserved for review and optional grading. |
| `citygml_dataset` | Yes | User-provided label associating the question with a CityGML dataset. |
| `category` | No | Optional query category for analysis/filtering. |
| `group_number` | No | Optional conversation-group identifier. Blank means a standalone question. |
| `sequence_number` | Conditional | Required for grouped rows; defines order within the group. |

`citygml_dataset` is supplied by the user because the dataset name cannot be
retrieved from 3DCityDB. It is used to select the relevant benchmark questions
for a run, but it does not automatically identify or switch databases, and it
is not silently injected into the model's prompt. Likewise, `category`,
`group_number`, and `sequence_number` are benchmark metadata, not question
content.

### Conversation grouping

- A row with a blank `group_number` is a fresh, single-turn test case.
- Rows with the same non-blank `group_number` form one conversation and run
  in ascending `sequence_number`.
- `sequence_number` must be present and unique within each group. Duplicate
  IDs, invalid sequence values, and incomplete group definitions should be
  rejected before model calls begin.
- Each repetition starts the group from its first question with empty
  conversation history. History is carried only between rows in that group.
- Group ordering should be deterministic even if the CSV rows are not already
  contiguous; output records should still preserve the original query IDs and
  sequence values.

This makes the options to retain a distilled summary or replay reasoning
meaningful for multi-turn groups while ensuring repetitions are independent.

## Selecting the Dataset and Database

A benchmark invocation selects one `citygml_dataset` value and runs the
matching rows against a 3DCityDB instance that already contains the intended
data. By default, the runner should use the same CityDB connection settings
as the existing application environment/.env configuration. An optional
command-line-selected environment/config file may override the DB connection
for a particular run.

The dataset label is an explicit user-maintained mapping, not something
discovered in the database. The operator is responsible for configuring the
connection to the instance containing that dataset. If multiple logical
datasets are mixed in one database and cannot be distinguished by existing
database fields or query logic, the benchmark cannot enforce data isolation
merely from the CSV label. This limitation should be stated clearly rather
than implying that the CLI switches or filters the database contents.

## CLI Layout and Interactive Chat

Provide both capabilities through one package/entry point with explicit
subcommands, for example:

```text
3dcitydb-chat chat [model and chat options]
3dcitydb-chat benchmark [CSV and benchmark options]
```

The `chat` subcommand is an interactive terminal alternative to the WebGUI,
or a complementary interface for users who prefer a terminal. It should
support the same OpenAI, Anthropic, and Ollama backends, optional
OpenAI-compatible `base_url`, and the same applicable temperature, thinking,
prompt, context, summary, and reasoning-replay settings. It should maintain a
conversation until the user exits or starts a new conversation. The
`benchmark` subcommand remains non-interactive and loads its models and tests
from CSV. Both subcommands use the shared runtime, so fixes to chatbot
behavior benefit the WebGUI and both CLI modes.

### Separating answer and reasoning/activity output

A terminal is a text interface, so reasoning or progress interleaved with the
answer could easily be mistaken for user-facing answer text. Use distinct
channels and persistent labels rather than relying on color or timing:

- By default, show the final answer without reasoning text.
- When explicitly enabled, show provider-emitted reasoning and/or an agent
  activity trace in a clearly marked **Reasoning / Agent activity — not the
  final answer** section. Tool calls and results can be shown as structured
  activity entries, separate from answer text.
- In interactive terminal mode, render trace and answer in separate,
  consistently labeled blocks. ANSI color may help visually, but the labels
  must remain clear when color is unavailable or disabled.
- In piped/non-interactive mode, write the final answer to stdout and progress
  plus optional reasoning/activity trace to stderr. This lets scripts capture
  the answer without accidentally including trace output.
- Make no promise that all providers expose the same reasoning content. When
  model reasoning is unavailable or disabled, the labeled tool/activity
  trace remains the consistent diagnostic view.

The CLI should describe these events accurately as provider-emitted reasoning
or agent/tool activity, not as a complete or uniform account of a model's
internal process.

## Benchmark CLI Settings

The `benchmark` subcommand should allow the user to configure:

- Model matrix and question CSV paths.
- Selected `citygml_dataset`, repetition count, and output file.
- Run-wide provider settings, with per-row model CSV overrides.
- Temperature, including leaving it unset to preserve the provider default.
- Thinking mode/level and prompt mode.
- Ollama context size and context-window cap.
- Whether to keep a distilled summary of how an answer was found.
- Whether to include previous reasoning steps in subsequent conversation
  context.
- Optional DB environment/config-file override.
- Optional independent evaluator provider, model, and OpenAI-compatible
  base URL.

The context monitor should record estimated tokens and the configured limit.
When a grouped conversation grows past the limit, the CLI should use the same
message trimming/capping policy as the WebGUI, and record whether and how
many turns were dropped. Any differences in context accounting between the
CLI and WebGUI should be treated as a defect in the shared-runtime refactor,
not an intended benchmark behavior.

Summary retention and reasoning replay follow the existing WebGUI semantics,
including provider-specific support. They primarily affect later turns in a
group, since each standalone question starts with a fresh conversation.

## Benchmark Execution and Result Recording

The initial implementation should run model/query/repetition cases
sequentially. This keeps database and provider load predictable and makes
latency measurements easier to interpret. Results should be appended and
flushed incrementally so completed work is not lost if a run is interrupted.

Each result row should contain enough information to reconstruct what was
tested and join it back to the exact input question:

| Result field | Purpose |
|--------------|---------|
| `run_id` | Identifies one benchmark invocation. |
| `query_file` and `query_file_sha256` | Identifies the source file and detects later edits. |
| `query_id` | Links directly to the source CSV row. |
| `citygml_dataset` | Records the selected dataset label. |
| `question`, `ground_truth` | Self-contained source query and reference answer. |
| `group_number`, `sequence_number` | Reconstructs grouped conversation order. |
| `provider`, `model`, `base_url` | Identifies the answering configuration; base URL excludes credentials. |
| `repetition` | Identifies the trial number. |
| `generated_answer` | Full answer emitted by the ChatBot. |
| `status`, `error` | Distinguishes completed answers from stopped/failed runs. |
| `elapsed_seconds` | Measures wall-clock time for the answer turn. |
| `context_tokens`, `context_limit`, `context_trimmed_turns` | Captures context pressure and cap behavior. |
| `evaluator_provider`, `evaluator_model`, `evaluator_base_url` | Identifies the separate grader configuration, when enabled. |
| `evaluation_result`, `evaluation_explanation` | Stores the grader output separately from the generated answer. |

The exact output format may be CSV for consistency with the inputs. Correct CSV
escaping, including multiline answers and ground-truth values, is essential.
One record per query turn, model configuration, and repetition is convenient
for analysis; grouped-turn records should also identify the conversation and
repetition they belong to.

Errors must remain explicit in the result file. A failed model call or tool
execution must not be recorded as an ordinary successful answer. API keys,
passwords, or other credentials must not appear in error strings, output
metadata, or logs.

## Optional LLM-Based Answer Comparison

Reference-answer comparison should be a separate feature from answer
generation. Configure its provider/model/base URL independently from
`models.csv`, using environment-based credentials. The evaluator must run in
its own isolated chat session and must never share or mutate the answering
model's conversation history.

For a given benchmark invocation, the same evaluator configuration and
evaluation prompt should be used for every answering model and repetition.
This allows answer-model differences to be studied against a fixed evaluator.
The evaluator should receive the question, ground truth, and generated answer
as comparison inputs; it should not receive hidden state from the answer
agent, such as its reasoning transcript, unless a future evaluation mode
explicitly calls for that.

Store the evaluator's model identity, raw response, and any parsed judgment or
score separately from the model answer. The evaluator is another LLM and its
judgment is not proof of correctness. Preserve enough raw output for manual
audit, and report evaluator failures independently without discarding the
generated answer.

## Phased Implementation Plan

### Phase 1 — Shared chatbot runtime

1. Identify the Gradio-independent orchestration currently inside
   `production/webui/app.py`.
2. Extract the turn/conversation behavior into shared non-UI code used by
   the WebGUI and both CLI subcommands, including message construction,
   prompt mode, provider dispatch, MCP tool execution, context
   estimation/trimming, summary behavior, and reasoning replay.
3. Keep Gradio and the CLI responsible for presentation and interaction only.
4. Add regression tests demonstrating that WebGUI behavior and backend event
   handling remain unchanged after extraction.

### Phase 2 — Interactive chat CLI

1. Add the `chat` subcommand as a terminal-based alternative to the WebGUI,
   using the shared conversation runtime and existing provider settings.
2. Keep the final answer separate from optional reasoning/activity output:
   labeled blocks in a TTY, stdout for the final answer and stderr for
   progress/trace when output is piped.
3. Test interactive conversation history, new-chat/exit behavior, provider
   configuration, and output separation, including operation without ANSI
   color.

### Phase 3 — CSV inputs and benchmark CLI

1. Implement model and query CSV readers with clear validation errors.
2. Validate provider values, required IDs/questions/ground truth/dataset
   labels, group/sequence consistency, numeric settings, and supported
   provider-specific options before starting a run.
3. Implement dataset selection and optional DB environment/config override.
4. Execute standalone queries and grouped conversations for each selected
   model and repetition using the shared runtime and a fresh conversation
   state at the start of each standalone query/group repetition.
5. Append result records incrementally, preserving query IDs, input file
   hash, model endpoint identity, answer, errors, timing, and context metrics.

### Phase 4 — Independent evaluator

1. Add separate evaluator provider/model/base URL configuration and prompt.
2. Invoke it in an isolated session after each generated answer is available.
3. Persist evaluator identity, raw comparison, and parsed structured output
   separately, while retaining the generated answer if evaluation fails.
4. Add tests proving the evaluator is consistent across answer models and
   never shares answer-session history.

### Phase 5 — Documentation and analysis workflow

1. Document both CLI subcommands, CSV schemas, configuration precedence,
   endpoint and DB setup, output columns, trace/answer stream behavior, and
   failure behavior.
2. Provide example analysis guidance for grouping results by dataset, query,
   provider/model/endpoint, and repetition.
3. Document that correctness can be reviewed manually or by the independent
   evaluator, and that evaluator scores should not be treated as ground truth.

## Testing Plan

Focused tests should cover:

- Model/query CSV parsing, malformed CSV, duplicate IDs, and invalid settings.
- Optional base URL propagation and distinguishing the same model used through
  OpenAI-compatible and Ollama backends.
- Query filtering by selected dataset and DB environment/config override
  behavior, without falsely claiming database data isolation.
- Independent standalone queries and fresh grouped conversations per
  repetition, with correct sequence ordering.
- Summary retention, reasoning replay, temperature, thinking level, prompt
  mode, context monitoring, and context capping propagation.
- Incremental CSV persistence, multiline CSV escaping, and stable linking to
  query ID and query-file hash.
- Explicit status/error records and interruption behavior.
- Interactive CLI conversation behavior, answer/trace channel separation,
  piped output semantics, and readable labels without color.
- Separate evaluator configuration/session, stable evaluator selection across
  all answer models/repetitions, and preservation of generated answers when
  grading fails.
- WebGUI regression coverage for the shared-runtime extraction.

## Decisions and Risks

| Area | Decision / consideration |
|------|--------------------------|
| Runtime reuse | Extract shared non-UI logic for the WebGUI, interactive `chat` CLI, and `benchmark` CLI; do not maintain separate chatbot implementations. |
| CLI output | Final answer and optional provider reasoning/activity trace use distinct labels and streams; trace is opt-in and never interleaved as if it were answer text. |
| Provider reasoning | Availability and shape differ by provider; agent/tool activity is the consistent diagnostic fallback. |
| Model endpoint | Per-row `base_url` enables OpenAI-compatible hosted/local endpoints; the same model can use distinct OpenAI and Ollama rows. |
| Secrets | Use environment/configuration for credentials; never include secrets in CSV or output. |
| Database | Use existing DB environment by default with an optional config-file override. Dataset label is user supplied; it does not select a database automatically. |
| Groups | Group number plus sequence number defines a conversation. Repetitions start from a fresh group state. |
| Execution order | Sequential initially; parallel execution can distort timings and increase API/database load. |
| Context cap | Must match WebGUI trimming behavior and expose measurements/trimming in output. |
| Grading | Fixed evaluator improves comparability but remains probabilistic and must not replace manual review or ground truth. |
| Input provenance | Store query ID and file hash so results remain traceable if the CSV later changes. |
| Endpoint identity | Record base URL as non-secret metadata; sanitize credentials if a URL ever contains them. |

## Not Covered in the First Version

- Automatically discovering or switching between CityGML datasets inside a
  database.
- Parallel benchmark execution or automatic retry policies that would alter
  provider load and latency measurements.
- A built-in dashboard for visualizing comparisons; result CSVs should first
  support external analysis.
- Treating LLM-grader output as an authoritative correctness score.
