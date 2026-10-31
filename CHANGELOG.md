# Changelog

All notable changes to the 3DCityDB MCP Server and ChatBot are documented here.
Both components are versioned together. The format is loosely based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This file was created
with 1.2.0; the 1.0.x and 1.1.0 entries below were reconstructed from the
published GitHub release notes and the commit history.

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
