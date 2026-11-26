# Changelog

All notable changes to the 3DCityDB MCP Server and ChatBot are documented here.
Both components are versioned together. The format is loosely based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This file was created
with 1.2.0; the 1.0.x and 1.1.0 entries below were reconstructed from the
published GitHub release notes and the commit history.

## [Unreleased]

### Added
- `CITYDB_EXTRA_SCHEMAS` (comma-separated schemas or `schema.table`
  entries): `assemble_prompt` adds an "Other Schemas" section, in full and
  compact mode, listing those tables, views and materialized views that the
  connected role can read, with column types (geometry type and SRID
  included) and their table and column comments, so queries can join tables
  kept beside 3DCityDB. Unset, the prompt is unchanged.

## [1.2.2]

### Changed
- Gradio web UI default port back to `7860` (was `7811` since 1.2.1;
  `GRADIO_PORT` still overrides).
- Default thinking level is `medium` for both the Ollama and the OpenAI
  provider (`high`/`max` break tool calling on small local models).
- Web UI refinements: the Settings collapse button sits on the "Settings"
  heading row (no wasted row), the "Scratchpad items" heading, the context
  bar next to "New conversation", a settings-collapse tab in the menu line,
  and an emoji rendering fix.
- Query-grain prompt rules (stop double-counting features with multiple
  addresses): RULE 0 asks for one row per feature on list questions
  (`GROUP BY f.objectid`, `array_agg` for multi-valued attributes; grouping
  by street also groups by city), RULE 1c treats a repeated objectid across
  rows as a query-grain error, and Pattern 9 is included only when the
  database actually contains addresses. The synthesized "buildings in
  street" example is fixed (it used a 1:N join without `GROUP BY`).
- Scratchpad 3D lines are drawn as plain polylines instead of Cesium
  `polylineVolume` pipes (the pipes crashed the viewer on purely vertical
  segments); `size_m` no longer applies to lines.
- Scene labels in the 3D viewer are drawn only for non-empty saved labels
  (the item list still shows the name).
- Scratchpad class `Surface3D` renamed to `CompositeSurface3D`; the
  `save_to_scratchpad` tool now distinguishes a connected 3D surface
  (`CompositeSurface3D`) from disjoint flat 3D polygons (`MultiPolygon3D`).
- Scratchpad prompt: the `[SCRATCHPAD]` block in the system prompt is short
  (3,307 -> about 860 characters) and says first when to use the tools (the
  user explicitly asks to create, draw, save, keep, mark or pin something, or
  to manage saved items) and when not: questions that find, list, count,
  compare or "show me" data are answered with `run_query` only. The how-to
  (columns, classes, boxes, heights, opacity) moved into the tool
  descriptions, which also start with the rule; the example that saved "tall
  buildings" (an ordinary question) is gone.
- Anti-hallucination rules for the chat: the `[SCRATCHPAD]` block gains a
  REPORT rule (an item may be described as saved/changed/deleted only after
  a scratchpad tool confirmed it in this turn), and the tool-use discipline
  now covers all side-effect claims (saved, changed, deleted, shown/hidden,
  queried) — a plan or a quoted tool schema is not a result.
- Wording: the `CG_3DAlphaWrapping` prompt line now says it creates a 3D
  polyhedral surface and that `relative_alpha` tightens the wrap.
- Bumped the MCP server, ChatBot and PyPI library version from 1.2.1 to
  1.2.2.

## [1.2.1]

### Security
- **HTTP/SSE transport requires authentication.** `3dcitydb-mcp-sse` now
  requires `Authorization: Bearer <token>` on `/mcp`, `/sse` and `/messages/`
  (`/health` stays open). The token is read from `CITYDB_MCP_AUTH_TOKEN`.
  Without it, the server only runs unauthenticated on a loopback address; on
  any other bind address it generates a token on first start, stores it in
  `mcp_auth_token` (mode 0600; `/app/data` in Docker, otherwise the user's
  config directory, or `CITYDB_MCP_AUTH_TOKEN_FILE`) and enforces it.
- **Safe defaults.** `--host` defaults to `127.0.0.1` (was `0.0.0.0`). The
  fullstack compose file publishes the MCP port on `127.0.0.1` only.
- **DNS-rebinding protection** for both HTTP transports: `Host` and `Origin`
  are checked; `localhost`, `127.0.0.1` and `[::1]` are accepted on any port,
  `CITYDB_MCP_ALLOWED_HOSTS` adds names.
- **No user impersonation over HTTP.** The HTTP server ignores the
  client-supplied `_ctx` session context, so HTTP clients use the shared
  `CITYDB_SCRATCHPAD_MCP_USER` scratchpad identity. The web UI (stdio) is
  unchanged.
- `/health` no longer returns database error details.
- Limits: `CITYDB_MCP_MAX_CONNECTIONS` (default 32) for the HTTP server and a
  per-statement `statement_timeout` (`CITYDB_STATEMENT_TIMEOUT_MS`, default
  600000 ms = 10 minutes, `0` disables) on every database connection.
- New `SECURITY.md` on reporting vulnerabilities privately.

### Added
- `save_to_scratchpad` can create **several items in one call**: an
  `item_name` column makes one item per value (e.g. one group per energy
  class, one marker per building), each with its own label and colour
  (`label` / `color` columns).
- Scratchpad **export and import** in the 3D viewer's item list (two icon
  buttons): export downloads all of the user's items as a JSON file that names
  the generator, the MCP server and chatbot versions and the export date and
  time (UTC); import adds the items of such a file (same names are replaced).
  The file is checked as untrusted input.

### Changed
- **Default ports moved off commonly used ones:** MCP HTTP/SSE server
  `8080` → `8811` (`--port`, `MCP_SSE_PORT`), Gradio web UI `7860` → `7811`
  (`GRADIO_PORT`). The compose files take both ports from these variables.
  Update your `.mcp.json` / client configuration and bookmarks.
- `.mcp.json` points at `http://localhost:8811/mcp/` and sends
  `Authorization: Bearer ${CITYDB_MCP_AUTH_TOKEN}`.
- Minimum `mcp` version raised to 1.10 (`mcp.server.transport_security`).
- Bumped the MCP server and ChatBot version from 1.2.0 to 1.2.1.
- The viewer's item list is English only ("My items"; it was German in
  German-language browsers) and stays visible when empty.
- The settings column is a sidebar that is closed by default (toggle at the
  left edge), so the chat and the 3D viewer get the whole width. "New
  conversation" and the context-window bar moved below the chat input.
- The viewer's selection panel opens from a pointer icon in the top-right
  toolbar (teal while selecting, with the number of selected features) instead
  of sitting bottom-left over the clock and timeline.

### Fixed
- "Load / Refresh prompt" shows the whole system message the model gets,
  including the scratchpad and flood-scene blocks (they were missing there and
  in the prompt sizes shown after a rebuild and in "Clear chat").
- A configured Ollama (`OLLAMA_BASE_URL`) stays the default provider when the
  server does not answer at startup (e.g. while it loads a model); the UI no
  longer falls back to anthropic with "No LLM provider configured".
- The context-window bar follows a change of provider, model or context size
  at once (it kept the previous provider's limit until the next turn).
- `save_to_scratchpad` takes `alpha` (opacity, e.g. 0.7 for 30 % transparency);
  it used to be dropped without a word, so the item kept 0.8 while the answer
  said otherwise. Unknown tool arguments are now an error, a `Polygon2D` on a
  one-part `MULTIPOLYGON` (an alpha shape) is stored as `MultiPolygon2D`
  instead of being refused, and a save reports the stored colour and opacity.
- The database prompt lists the 2D alpha shape (`CG_OptimalAlphaShape`) and
  `CG_3DConvexHull`; `CG_3DAlphaWrapping` and extruding an area are probed at
  startup and replaced by a workaround note where this PostGIS cannot run them. The note
  says that only the 3D wrapping is missing, the 2D alpha shape works.
- Context window of Ollama models: the model family's `<family>.context_length`
  is read (e.g. `gptoss.context_length`; only `llama.` / `general.` were), and
  Ollama models in OpenAI mode use the server's context (the size the model is
  loaded with, else its maximum) instead of a fixed 200,000.

## [1.2.0]

### Added
- **Spatial scratchpad** — a per-user "staging area" the chatbot fills with
  saved, reusable results: *featuresets* (named sets of buildings) and
  *geometries* (pins, points, lines, draped 2D areas, extruded 3D solids,
  convex hulls) with per-item name, colour, opacity and 2D/3D class. Managed
  through four new MCP tools (`save_to_scratchpad`, `update_scratchpad_item`,
  `scratchpad_action`, `list_scratchpad`) and a per-user `my_scratchpad` SQL
  view, isolated by a read-only database role and a `security_barrier` view.
  Saved items are shown in the 3D viewer (bottom-right "My items" list) and can
  be shown, hidden, re-styled, re-flown-to and deleted; every change is written
  to a JSONL audit log. Opt-in via `CITYDB_SCRATCHPAD=true`.
  See `production/docs/scratchpad.md`.
- **Session layer** — per-user identity (a uuid in the browser's
  `localStorage`, no login), per-tab Stop, and parallel chat turns, replacing
  the previous process-wide globals. A sanitized user context is passed to MCP
  tool calls (`_ctx`) and is the basis for the scratchpad's per-user isolation.
  Opt-in concurrency via `WEBUI_CONCURRENCY`. See
  `production/docs/session-layer.md`.
- **Flood scene** — a "bathtub" flood visualization in the 3D viewer, triggered
  by a `flood_level_m` column in a query result: a water surface is drawn at
  that height and every building is coloured red (flooded), green (protected by
  a `protection_m` barrier) or grey (dry), with a level slider, gauge and
  warning levels. Works with any model, including local Ollama. Opt-in via
  `FLOOD_SCENE=true`. See `production/docs/flood-scene.md`.
- **"Show every row in result tables" chat setting** — toggle (default on) to
  control whether long query results list every row or show a few example rows
  plus a collapsible CSV/JSON attachment.
- **README** — split the Features section into *MCP Server* and *ChatBot*
  features; added dedicated sections advertising the scratchpad and flood
  scene; extended the coordinate-reference-system table with Dimensionality and
  Units-of-measure columns plus Japanese PLATEAU (EPSG:6697), Dutch
  (EPSG:7415) and US survey-foot (EPSG:32614) entries.

### Changed
- Bumped the MCP server and ChatBot version from 1.1.0 to 1.2.0.

## [1.1.0]

### Added
- **LLM benchmarking tool suite** — a new, reusable suite for repeatable
  multi-LLM evaluation of the chatbot:
  - **Benchmark runner** — run a set of queries (with ground-truth answers)
    from a `questions.csv` against multiple LLMs (OpenAI, Anthropic, Ollama)
    configured in a `models.csv`; each query is repeated N times and results
    are written to a CSV for comparison. Lines starting with `#` in the CSVs
    are skipped (line numbers are still counted).
  - **CLI chatbot** (`3dcitydb-chat`) — behaves exactly like the WebGUI
    chatbot (OpenAI / Anthropic / Ollama, distilled context summaries,
    optional reasoning steps, temperature, thinking mode, prompt mode, and live
    context-size monitoring with capping) and shares its chat runtime with the
    WebGUI, so improvements apply to both.
  - **Grading web app** — grade benchmark results in the browser
    (correctness, completeness, relevance, clarity, language conformity +
    comments), edit ground-truth answers, and jump between results
    (Previous / Next, ±20, next ungraded).
  - **Answer-story column** — benchmark result CSVs gain a column with the
    distilled "how the answer was found" story (or a note when the answer came
    from context / built-in knowledge).
  - Documentation in `production/docs/benchmarking.md`, including how to run
    benchmarks and the grading app in Docker.
- **Shared chat runtime** extracted between the WebGUI and the new CLI, keeping
  their behaviour in lockstep.

### Changed
- Bumped the MCP server and ChatBot version from 1.0.1 to 1.1.0.

## [1.0.1]

Patch release fixing the HTTP/SSE deployment for modern MCP clients.

### Added
- **Streamable HTTP transport** — a new endpoint `http://your-server:8080/mcp`
  for modern MCP clients (e.g. VS Code), served by the same MCP server as the
  existing `/sse` endpoint. Both run in the same process; `/sse` and `/health`
  are unchanged.

### Fixed
- **405 Method Not Allowed for modern MCP clients** — modern clients speak the
  Streamable HTTP transport (they POST to the configured URL and only fall back
  to SSE on a 404). The 1.0.0 SSE server only accepted GET on `/sse`, so such
  clients got `405` and never connected. Point a modern client at
  `http://localhost:8080/mcp/`.

### Changed
- Bumped the MCP server and ChatBot version from 1.0.0 to 1.0.1.

No breaking changes; stdio and Gradio-UI deployments are unaffected.

## [1.0.0]

The headline feature: a **3D viewer for the Chat Assistant** (new deployment
option 3b).

### Added
- **Deployment option 3b — Fullstack + 3D viewer.** Layering
  `docker-compose.fullstack.viz.yml` on top of the fullstack stack embeds the
  3DCityDB Web Map Client (Cesium) into the Chat tab. The model is streamed as
  3D Tiles generated on demand by a `citydb-3dtiler` container.
- **Answer highlighting** — features listed in an answer are highlighted in 3D
  and the camera flies to them.
- **Viewer-to-chat selection** — toggle selection mode, click features (with a
  panel to pick the right level: a wall, its building, its siblings), and the
  chatbot receives the selection as a `[VIEWER SELECTION]` context block so
  questions can say "these buildings" / "die ausgewählten Objekte".
- **New MCP tools for the viewer** (offered only when `ENABLE_VIZ=true`):
  `resolve_highlight_targets`, `get_feature_tree`, and `describe_selection`.
- **Viewer configuration** via `VIEWER_TERRAIN_*` and `VIEWER_WMS_*`
  environment variables (defaults: basemap.de DGM5 terrain + WMS base map).

### Changed
- **WebUI:** live progress for system-prompt re-assembly (per-step progress
  streamed, button disabled while running, scroll preserved); the post-import
  prompt rebuild is deferred and offered via a popup; the MCP Inspector now
  reads the live server self-description and tool list from the running MCP
  server instead of hard-coding them.
- **MCP server:** `assemble_prompt` and `run_query` are listed first in
  `list_tools()` so clients present the two primary tools up top.
- Bumped the MCP server, Python package, and Chat Assistant to **1.0.0**.

### Fixed
- **Firefox 3D viewer crash** — starting the Cesium viewer while its iframe is
  hidden (e.g. right after an import) broke the render loop in Firefox. The
  viewer now starts — and tile reloads — only once the viewer tab is visible.
- Stale highlights / camera no longer survive a tile reload; the start-view
  zoom uses `zoomToStartPosition`.
