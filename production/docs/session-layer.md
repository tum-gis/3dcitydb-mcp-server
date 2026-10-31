# Session layer (users, tabs, turns)

The web UI knows three levels: the **user**, the browser **session** (one tab) and the **turn**
(one question). Stop, concurrency, tool calls and per-user data hang off these levels instead of
off process-wide globals. This is "Phase 0" for the per-user features planned in the
`idea-for-future-*` documents.

## Why

Before this change:

- Stop was process-wide (`_stop_event` / `_active_future` globals in `production/webui/llm_utils.py`).
  Stop in one browser tab aborted another user's answer, and the `_clear_stop()` at the start of a
  new turn swallowed someone else's Stop.
- `demo.queue()` in Gradio 5 runs one chat at a time, so users waited for each other.
- MCP tool calls carried no notion of the user.

The planned per-user features need the third point: the spatial scratchpad
([idea-for-future-spatial-scratchpad.md](idea-for-future-spatial-scratchpad.md)), RAG memory, file
uploads and web search.

## The three levels

| Level | Identity | Lifetime | Owns |
|-------|----------|----------|------|
| user | `user_id`, a uuid in the browser's `localStorage['citydb_user_id']` (no login) | until the browser data is cleared, or the server expires it | the user folder, later: scratchpad rows, memory |
| session | Gradio's `request.session_hash` (one browser tab) | until the tab is closed | the binding tab -> user, the running turn |
| turn | `TurnControl.turn_id` (12 hex chars) | one question | its own stop flag and in-flight LLM future |

All code is in `production/webui/session.py`.

## Step 1: `production/webui/session.py`

- `TurnControl`: `stopped()`, `stop()` (sets the flag and cancels the registered future),
  `set_future(fut)`. Backends poll `stopped()` and register their LLM future so Stop can cancel it.
- `UserContext(user_id, session_id, turn_id)` and `to_tool_ctx()`, which returns the dict sent to
  the MCP server (see step 4).
- Registry (in-memory, lock protected): `register_user(session_id, raw_id)`, `user_for(session_id)`,
  `forget_session`, `begin_turn` (returns `(UserContext, TurnControl)`), `end_turn`, `stop_session`.
  `end_turn` only removes the turn if it is still the tab's current one.

The turn is passed **explicitly** as a parameter, not through `contextvars`. Gradio runs the steps
of a generator handler in changing worker threads, so a context variable set at the start of the
generator is not reliably visible later.

## Step 2: Stop per tab

- The globals are removed from `llm_utils.py`.
- A `turn` parameter is threaded through `chat_runtime.stream_chat_turn` ->
  `backends/__init__.stream` -> `backends/cloud.native_tool_stream` and
  `backends/local.react_stream`. Both backends create a fresh `TurnControl` if none is given, so the
  benchmark CLI (`production/webui/cli.py`) needs no `turn`. (With the scratchpad on, the CLI now
  sends its own per-run user id as `_ctx`; see [scratchpad.md](scratchpad.md#cli-and-benchmark).)
- In `production/webui/app.py`, `chat_stream` is now a thin wrapper: it takes the `gr.Request`,
  calls `begin_turn`, `touch_user`, delegates to `_chat_turn`, and calls `end_turn` in `finally`.
- The Stop button calls `stop_session(request.session_hash)`, so it only stops the turn of its own tab.

## Step 3: Identity in the browser

- `_USER_ID_JS` (in `app.py`) runs on `demo.load`. It reads `localStorage['citydb_user_id']`, creates
  a uuid with `crypto.randomUUID()` if there is none, and sends the id to the server.
- `_on_register_user` binds the id to the tab's session (`register_user`). An invalid id is replaced
  by a fresh uuid.
- `demo.unload(_on_unload)` calls `forget_session`, which also stops a running turn of the closed tab.
- Tabs of the same browser share one user id. A private window or another browser is a different user.

## Step 4: Context into tool calls

- `production/webui/mcp_client.py`: `run_tool(..., ctx=None)` and `run_tool_sync` add a reserved
  argument `_ctx = {user_id, session_id, turn_id}` to the tool arguments. Only the web UI sets it;
  the LLM only ever produces the `sql` string.
- `src/citydb_mcp/tool_context.py` and `call_tool` in `src/citydb_mcp/server.py` strip `_ctx` before
  dispatch, so existing tools see unchanged arguments. The sanitized context is available through
  `current_tool_ctx()` (an empty dict outside the web UI).

A future per-user tool would use it like this:

```python
from .tool_context import current_tool_ctx

def _scratchpad_add(arguments: dict) -> str:
    user_id = current_tool_ctx().get("user_id")
    if not user_id:
        return json.dumps({"error": "no user context (call from the web UI)"})
    # ... INSERT INTO scratchpad (user_id, ...) VALUES (%s, ...)
```

## Step 5: Concurrency

`demo.queue(default_concurrency_limit=WEBUI_CONCURRENCY)` on both launch paths in `app.py`
(default 4). Several users can now have a turn running at the same time.

## Step 6: Per-user folder and expiry

- `user_dir(user_id)` returns `$CITYDB_USER_DATA_DIR/<uuid>` (default `/app/data/users`). That is
  inside the already mounted `./data` volume, so it survives container restarts. Only uuids are
  accepted, so there is no path traversal.
- `touch_user` writes a `.last_seen` file once per turn.
- `register_purge_hook(fn)`: `fn(user_id)` is called when an inactive user is purged. Future features
  use it to delete their rows. Exceptions in a hook are logged and do not stop the purge.
- `purge_inactive_users(days)` deletes the folders of users not seen for `days` and skips users that
  have an open tab. `start_purge_timer()` runs it at startup and then every 24 h.

## Configuration

Also listed in `production/.env.example`.

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBUI_CONCURRENCY` | `4` | How many chat turns may run at the same time |
| `CITYDB_USER_DATA_DIR` | `/app/data/users` | Root of the per-user folders |
| `CITYDB_USER_EXPIRY_DAYS` | `14` | Days of inactivity before a user's data is deleted; `0` = off |

## How later features plug in

- **Spatial scratchpad**: rows keyed by `current_tool_ctx()["user_id"]`; a purge hook deletes the
  user's rows.
- **RAG memory**: a `user_id` column on the memory table, optionally with a shared scope.
- **File uploads**: stored in `user_dir(user_id)`.
- **Web search**: per-user rate limit and logs; downloads go into `user_dir(user_id)`.

## Limits and security notes

- The localStorage id is **not authentication**. It is a random bearer key: whoever has the uuid is
  that user. That is fine for low-value data. Before storing personal content (uploads, memory)
  outside the lab, put a real login in front (e.g. a reverse-proxy auth) and take `user_id` from its
  header instead. Only the source of the id changes.
- `_ctx` over the SSE endpoint can be set by any MCP client. It is the caller's own context, not a
  privilege.
- There is no database-level isolation yet. That is the scratchpad's job.
- `_prompt_stale` and the caches stay process-wide on purpose: they describe the schema and dataset,
  not the user.
- With the local Ollama agent, the worker thread may finish its current LLM call in the background
  after Stop (pre-existing behaviour). The turn itself ends immediately.

## Testing

`tests/test_session_layer.py` covers:

- Stop only hits its own session; a new turn does not clear another session's Stop; Stop cancels the
  registered future; `end_turn` keeps a newer turn of the same tab.
- `register_user` keeps valid ids and replaces invalid ones; an unknown tab gets a fresh id; `forget_session`
  stops its turn.
- Shape of `to_tool_ctx()`; `user_dir` creation and rejection of non-uuids.
- Purge: removes only inactive, offline users, runs hooks, skips users with an open tab, survives hook errors.
- `split_tool_ctx` strips and sanitizes `_ctx`; `current_tool_ctx` is scoped to the call.
- `mcp_client` adds `_ctx` only when given; the dispatcher forwards the turn; the cloud backend exits
  when its turn is stopped.

```
pytest tests/test_session_layer.py -v
```

Manual check:

1. Open the UI in a normal and in a private window and ask a question in both at the same time. Both
   answer in parallel.
2. Click Stop in one window. Only that answer stops.
3. The logs show `[session] tab ... -> user ...` on page load and `[session] turn ... user=...` per question.
4. Reload the page. The log shows the same user id again.
