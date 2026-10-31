# Changelog

All notable changes to the 3DCityDB MCP Server and ChatBot are documented here.
Both components are versioned together. The format is loosely based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this file tracks
released versions from 1.2.0 onward (earlier 1.x history predates the file).

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
