# Idea: Session-Scoped Chat Cancellation for Concurrent WebGUI Users

## Motivation

The WebGUI is intended to serve multiple employees using the same CityDB
instance and shared database configuration. Different users may choose
different providers or models, ask separate questions, and expect their
conversations to proceed independently. They do not need separate database
settings or database instances.

Most conversation data is already maintained in per-browser Gradio state:
history, reasoning/story history, tool-result cache, and viewer selection are
not intentionally shared between users. Provider, model, temperature,
thinking level, prompt mode, and other controls are passed as inputs for each
chat event. Shared prompt caches, MCP event-loop infrastructure, model worker
pool, and the database connection are process-wide resources by design.

The exception of concern is chat cancellation. `production/webui/llm_utils.py`
currently has one module-level stop event and one module-level active future.
The Chat Stop button calls `request_stop()` without identifying the browser
session. If two chat operations overlap, a stop request can signal the shared
event and affect work other than the operation whose Stop button was pressed.
The active-future slot can likewise only refer to one future at a time.

Gradio queues events, and its default concurrency behavior may serialize
calls to an individual event listener. However, the chat can be started
through separate submit and button event listeners, and queue serialization
is not a substitute for session-scoped cancellation. The cancellation code
should remain correct if operations overlap, if queue configuration changes,
or if multiple event paths invoke the same chat runtime.

## Desired Behavior

- One user's Stop action affects only that user's active chat operation.
- Stopping or finishing one session does not clear, replace, or cancel another
  session's cancellation state.
- Each invocation has well-defined cancellation state and active provider
  work handles.
- Repeated Stop clicks are safe, and stale session/run registrations are
  cleaned up after completion, stop, exception, or disconnect.
- A user's provider/model selection and conversation state continue to be
  independent as they are today.
- Shared read-only database access and shared application-level prompt/cache
  resources remain shared; this change is not a proposal for per-user
  database configuration.

## Current Implementation

### Per-session state

`production/webui/app.py` creates `gr.State` values for the chat history,
activity log, tool cache, reasoning history, distilled-story history, and
viewer selection. The send callback receives the active provider, model,
temperature, thinking setting, prompt mode, and context-window setting as
event inputs. This is the basis for separate user conversations and
per-request LLM configuration.

### Process-wide cancellation

In `production/webui/llm_utils.py`:

- `_stop_event` is a single `threading.Event` for the whole Python process.
- `_active_future` is a single process-wide future, guarded by a lock.
- `request_stop()` sets that event and cancels that future.
- `_clear_stop()` clears the same event at the start of a chat turn.

The cloud backend polls the shared stop state while waiting for completion
futures. The Ollama/ReAct backend polls the same stop state while consuming
agent events. Because these are globals, one invocation can clear or set
cancellation state observed by another overlapping invocation.

## Proposed Design

Make cancellation state scoped to a chat run and route Stop actions to the
current session's run.

One possible structure is a small cancellation/run context containing:

- A per-run `threading.Event`.
- The active provider future, if the backend currently has a cancellable
  future.
- A run identifier and, where needed, the associated Gradio session hash.
- Thread-safe methods to request cancellation, check cancellation, register
  the active future, and clear the future only if it still belongs to that
  run.

The chat callback should create or acquire the run context and pass it
explicitly through the backend dispatcher into the cloud and local backends.
Backend checks and active-future registration then use that context instead
of module-global `_is_stopped()` and `_set_active_future()` state.

The Stop callback should identify the request's Gradio session (for example,
using the request/session information provided to a Gradio callback) and ask
the run registry to stop only that session's current run. Protect the
registry with a lock. Do not use a single global future as a fallback: that
would reintroduce cross-session cancellation.

The exact registry key and lifecycle need validation against the deployed
Gradio version. A request/session key may be sufficient if the UI guarantees
at most one active chat run per session. If overlapping requests from the
same browser session are possible, key by both session and run ID, and define
whether Stop cancels the latest/current run or all active runs for that
session. Prefer preventing two simultaneous turns in one session if the UI
already assumes serial conversation history.

Avoid relying solely on mutable objects stored in `gr.State` unless Gradio's
copying and callback concurrency semantics are verified for the pinned
version. A process-local, lock-protected registry keyed by session/run
identity is an alternative, with explicit registration and cleanup in
`try/finally`.

## Cancellation Semantics and Limits

Cancellation should be cooperative: set the run's event, have the backend
stop polling/yielding work for that run, and cancel a provider future if it
has not started. A Python `Future.cancel()` generally cannot terminate a
function that is already running, and it does not guarantee termination of an
HTTP request already sent to a provider. The UI can stop waiting and discard
late output, while the underlying provider operation may continue until its
request timeout or response. Stronger transport cancellation would require
provider-client support and should be treated as a separate enhancement.

The implementation should report a user-visible stopped state for the
selected run and ensure any eventual late result cannot update a later turn
or another user's UI. Cleanup must happen even when a backend raises or a
browser disconnects.

## Implementation Requirements

1. **Audit cancellation call paths.** Enumerate all reads/writes of
   `_stop_event`, `_active_future`, `_is_stopped()`, `_clear_stop()`,
   `_set_active_future()`, and `request_stop()` across both backends and the
   WebGUI. Preserve the distinction between cancelling a provider request and
   stopping the agent loop.
2. **Introduce run-scoped cancellation.** Add a typed context/API and pass it
   through `chat_stream`, `backends.stream`, and both backend implementations.
   Preserve current behavior for one user while eliminating shared stop/future
   state.
3. **Associate Stop with the current session/run.** Obtain and validate
   session identity from Gradio's callback request, register active run state,
   and route Stop to only the matching entry. Define behavior for no active
   run, duplicate Stop clicks, overlapping same-session events, and
   disconnects.
4. **Clean up safely.** Use `try/finally` to unregister completed or failed
   runs. Ensure an old run cannot clear a newer run's future or registry slot.
5. **Review queue behavior.** Keep the fix correct regardless of whether
   Gradio serializes ordinary submissions; do not depend on queue limits as
   the cancellation-isolation mechanism. Consider explicitly sharing a
   concurrency ID or disabling duplicate in-session submits only if needed
   to protect ordered chat history.
6. **Add regression tests.** Use two independent cancellation contexts and
   overlapping mocked backend runs to verify stopping session A leaves B
   running, and vice versa. Test cleanup after normal completion, exception,
   Stop before/after future registration, duplicate stop, and stale-run
   cleanup. Include a test that an older run cannot unregister a newer run.
7. **Verify in Gradio.** Check the deployed Gradio version's request/session
   identity and concurrent callback behavior. Exercise two browser sessions
   with different provider/model settings and overlapping turns; stop one and
   confirm only its UI/run stops.

## Benefits

- Users can work against the same city model concurrently without a Stop
  button in one browser affecting another user's query.
- Different users can choose different LLMs or endpoints independently for
  their own requests.
- Cancellation behavior becomes explicit, testable, and robust to additional
  event listeners or queue configuration changes.
- Run-scoped cancellation can also be reused by a future interactive CLI or
  benchmark runner without inheriting a process-global stop flag.

## Scope and Non-Goals

- This change does not add authentication, per-user database access, or
  separate database settings.
- It does not isolate shared prompt caches, MCP connections, thread pools, or
  the common database; those remain intentional shared resources, subject to
  their own concurrency safety.
- It does not promise to forcibly terminate a provider request already in
  progress; that depends on client/transport cancellation support.
- It does not change answer generation, prompts, or provider selection.

## Effort Estimate

The basic refactor is likely a few hours to half a day: introduce per-run
cancellation state, thread it through the backend signatures, and route the
Stop callback to the relevant active run.

Allow up to about a day for a robust change with race-focused unit tests,
Gradio session identity verification, lifecycle cleanup, and a two-browser
manual check. Provider-side termination of already-running requests may
require separate investigation and is not included in that estimate.
