# Idea: Support for Multiple MCP Servers in the ChatBot

## Motivation

The WebUI currently talks to exactly **one** MCP server: `3dcitydb-mcp`
(spawned as a stdio subprocess). Supporting additional MCP servers
(e.g. web search, file access, weather — see
[idea-for-future-websearch.md](idea-for-future-websearch.md)) would make the ChatBot a general-purpose
agent shell instead of a single-database assistant, while the same plumbing
also makes any future server a few lines of `.env` away.

## Current State: Hard-Wired to One Server, One Tool

Three layers bake in the "one server, one tool" assumption:

### 1. MCP client layer — single hard-coded server

`production/webui/mcp_client.py`:

```python
@asynccontextmanager
async def mcp_session():
    params = StdioServerParameters(
        command="3dcitydb-mcp",
        args=[],
        env=os.environ.copy(),
    )
    async with stdio_client(params) as (read, write):
        ...
```

No config parsing, no server list, no server selection. Every call
(`run_tool_sync`, `assemble_system_prompt_sync`) goes to this one server.
Note `env=os.environ.copy()` — anything in `.env` already flows into the
child process, so additional servers inherit DB credentials etc. for free.

### 2. Agent backends — hard-wired to `run_query` + SQL

- `production/webui/backends/cloud.py` (Anthropic/OpenAI, native tool
  calling): passes a single function schema, `tools=[RUN_QUERY_TOOL]`, and
  validates every tool call as `{"sql": "..."}` before executing
  `tool_executor(sql)`.
- `production/webui/backends/local.py` (Ollama, ReAct text parsing): the
  hardest part — a hand-rolled parser that looks for
  `Action: run_query` / `Action Input: {"sql": ...}`, recovers SQL written
  in prose, and even *coerces* unknown tool names into `run_query`. Its
  system prompt is CityGML-SQL instructions.
- `production/webui/app.py` wires the loop with a single-tool executor:
  `tool_executor=lambda sql: run_tool_sync("run_query", {"sql": sql})`.

### 3. 3DCityDB-specific UI / prompt assumptions

- The system prompt comes from the citydb server's proprietary
  `assemble_prompt` tool — other servers have no equivalent hook.
- Status bar probes citydb tools (`get_lod_config`, `get_server_version`).
- The "last tool result" cache (`_build_tool_cache`) and Agent Activity
  rendering (`_render_tool_call` emits a ```sql fence) assume every tool
  call is a SQL query returning `preview_rows`.

## Proposed Design

### Server configuration via `.env`

Per-server variable blocks (more ergonomic than a JSON blob in `.env`):

```dotenv
MCP_SERVER_CITYDB_ENABLED=true
MCP_SERVER_CITYDB_COMMAND=3dcitydb-mcp

MCP_SERVER_WEATHER_ENABLED=true
MCP_SERVER_WEATHER_COMMAND=uvx
MCP_SERVER_WEATHER_ARGS=weather-mcp
```

Parsed by scanning `os.environ` for the `MCP_SERVER_<NAME>_` prefix.

### Code changes

1. **`mcp_client.py` — server registry.** Replace the single `mcp_session()`
   with one session/context per configured server, plus a multi-server
   signature `run_tool_sync(server, tool, args)` (or a tool→server routing
   table). Keep the existing background event-loop design — it is already
   concurrency-safe.

2. **Tool discovery & routing.** At startup, call `session.list_tools()` on
   each server and build a flat map:

   ```python
   ROUTE = {"run_query": "citydb", "get_forecast": "weather"}  # tool → server
   TOOLS = [<merged OpenAI function schemas derived from list_tools()>]
   ```

   Handle **name collisions** between servers (e.g. prefix tool names with
   the server name).

3. **Backends.**
   - `cloud.py`: replace `tools=[RUN_QUERY_TOOL]` with the merged `TOOLS`
     list; replace the `args["sql"]` validation with generic JSON-args
     dispatch via `ROUTE[tool_name]`. Also generalize the "SQL-in-text
     fallback" path (or disable it for non-SQL tools).
   - `local.py`: the big one — either restrict the local path to one
     "primary" server (recommended first step) or generalize the ReAct
     parser to accept arbitrary tool names and JSON arguments.

4. **System prompt.** Extend `assemble_system_prompt` to append generic
   tool documentation (name / description / input schema from
   `list_tools()`) for non-citydb servers; the citydb prompt stays
   server-provided.

5. **UI.**
   - `_render_tool_call`: render arbitrary tool name + JSON args, not just
     SQL fences.
   - `_build_tool_cache`: make SQL-specific caching opt-in per tool.
   - Status checks (`_check_mcp_status`, `get_mcp_version`): per-server
     instead of probing citydb tools.

## Phased Approach

**Phase 1 — "cloud backend only" (recommended starting point).**
Change only `cloud.py` (+ `mcp_client.py` registry): Anthropic/OpenAI models
get all servers' tools; the Ollama path stays SQL-only (its system prompt
and parser untouched). This is the lowest-risk route — roughly a day of work
instead of a week — because native tool calling needs no text parsing, and
the CityGML prompt in `local.py` still fits perfectly.

**Phase 2 — generalize `local.py`.**
Extend the ReAct parser to accept arbitrary `Action: <tool>` /
`Action Input: <json>` pairs, validate args against the discovered schemas,
and remove the "coerce to run_query" fallback. Only then can small local
models use the extra servers.

## Trade-offs

| Aspect | Consideration |
|--------|---------------|
| Tool-name collisions | Two servers exposing `search` → need prefixing or a naming convention at discovery time |
| Context budget | More tools = longer tool-schema section in every request; with a 64K window this is fine for a handful of servers, but should be monitored |
| Model reliability | Weak models confuse extra tools; `tool_choice="auto"` may pick the wrong one. Phase 1 keeps local models safe from this |
| Status/observability | One "MCP server" status dot becomes N; consider a per-server list in the status bar |
| Prompt provenance | `assemble_prompt` is a citydb-specific concept; the merged system prompt needs a stable section structure (citydb schema first, extra tools last) |
| Failure isolation | One broken extra server must not take down the whole chat — spawn/initialize failures should degrade to "server unavailable" with a status indicator, not crash startup |

## Not Covered Here

- Which *specific* extra servers to add (see [idea-for-future-websearch.md](idea-for-future-websearch.md)
  for the web-search / document-inspection use case).
- MCP server management UI (enable/disable at runtime) — `.env` + restart
  is sufficient for now; hot-reload would require session teardown/reconnect
  logic.

## Estimated Effort

| Piece | Effort |
|-------|--------|
| Server registry in `mcp_client.py` + `.env` parsing | ~half a day |
| Tool discovery, merging, collision handling | ~half a day |
| `cloud.py` generic tool dispatch | ~half a day |
| `app.py` dispatcher + UI rendering + status bar | a few hours |
| **Phase 1 total** | **~1–2 days** |
| `local.py` ReAct parser generalization (Phase 2) | ~2–3 days (parser + prompt + tests) |
