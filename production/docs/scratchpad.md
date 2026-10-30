# Spatial scratchpad (saved results in the 3D viewer)

The scratchpad is a per-user list of saved results that the chatbot creates and the 3D viewer shows.
An item is either a **featureset** (a set of objectids, e.g. "the buildings affected in 2013",
coloured in the tiles) or a **geometry** (a pin, line, area or solid, e.g. "my house" or a hull around
a cluster). The model saves with four MCP tools (`save_to_scratchpad`, `update_scratchpad_item`,
`scratchpad_action`, `list_scratchpad`), which also work from external MCP clients such as Claude Desktop or VS Code.
Saved items can be queried again later, hidden, shown, restyled, deleted and flown to. Every change is written to an [audit log](#audit-log).

It builds on the [session layer](session-layer.md): items are keyed by the user id that the web UI
passes to the MCP server with every tool call, and they are deleted by the session layer's purge hook.
The original design is kept in [idea-for-future-spatial-scratchpad.md](idea-for-future-spatial-scratchpad.md).

## Enable it

In `production/.env`:

```
CITYDB_SCRATCHPAD=true
# Optional: name and password of the read-only query role (defaults below)
# CITYDB_QUERY_USER=citydb_query
# CITYDB_QUERY_PASSWORD=<derived from CITYDB_PASSWORD and CITYDB_NAME if unset>
# Optional: shared scratchpad user id for MCP clients without a web UI session
# CITYDB_SCRATCHPAD_MCP_USER=<default: a fixed uuid>
# Optional: audit log, one JSON line per change; empty switches it off
# CITYDB_MCP_AUDIT_LOG=/app/data/audit/scratchpad.jsonl
```

- On startup the web UI calls `provision()` (`src/citydb_mcp/tools/scratchpad.py`) in a background
  thread. It is idempotent: it creates the table, a GiST index, the view `my_scratchpad` and the role.
- `CITYDB_SCRATCHPAD=true` also adds one `[SCRATCHPAD]` block to the system prompt
  (via `extra_prompt_blocks()` in `production/webui/llm_utils.py`), so the web UI and the benchmark CLI
  see the same prompt (and get the same tools, see [CLI and benchmark](#cli-and-benchmark)). The
  block is `SCRATCHPAD_TOOLS_INSTRUCTIONS`.
- The MCP server (`src/citydb_mcp/server.py`) lists the four scratchpad tools only when
  `CITYDB_SCRATCHPAD=true`; `_execute_tool` dispatches them to `scratchpad.run_tool`.
- The 3D viewer (`ENABLE_VIZ=true`) is needed to **display** items. Without it, saving and querying
  still work; only the viewer payload is skipped.
- The derived query password is `sha256("citydb_query:<CITYDB_PASSWORD>:<CITYDB_NAME>")[:32]`, so the
  web UI and every MCP subprocess agree without storing another secret. Set `CITYDB_QUERY_PASSWORD`
  to use your own.
- `provision()` needs `CREATEROLE`. The fullstack setup has it (bootstrap superuser). With BYOD and
  fewer rights, the table and view are still created, the role is not, and a fallback guard is used
  (see "Isolation between users").

## How the chatbot uses it

The model saves and manages items only through the **MCP tools**. `run_query` does not save anything:
it is read-only, and the web UI only passes the user context to it so that `my_scratchpad` shows the
user's items.

### MCP tools

`TOOL_SCHEMAS` in `scratchpad.py` is the single description used by the MCP server, the cloud backend
and the local ReAct backend.

| Tool | Arguments | Does |
|------|-----------|------|
| `save_to_scratchpad` | `name`, and `sql` **or** `geojson`; optional `label`, `color`, `clamp`, `class`, `size_m`, `height_m`, `extrude_m`, `base_z` | Saves an item (replaces an existing name). `sql` is a SELECT with an `objectid` column (featureset) or a `geom` column (geometry); if both are present, `geom` wins. `geojson` is a Geometry, Feature or FeatureCollection in WGS84 lon/lat; it is converted to the database CRS (`_geojson_sql` passes the geometries as quoted literals, never as free SQL). |
| `update_scratchpad_item` | `name`; optional `label`, `color`, `alpha`, `class`, `size_m`, `clamp`, `height_m`, `extrude_m`, `base_z` | Changes a saved item in place: style, class and height (see below). |
| `scratchpad_action` | `action` (`show`, `hide`, `delete`, `goto`), `names`; optional `confirm` | Acts on the named items. Deleting more than one needs `confirm: true` (see below). |
| `list_scratchpad` | none | Name, label, kind, class, colour, visibility, count or geometry type. |

`run_tool(name, arguments, ctx_user_id)` executes them and returns `{ok, name, kind, count, type}` or
`{ok: false, error}`. `type` is the stored geometry type with ` Z` when it has heights (`POINT Z`,
`POLYHEDRALSURFACE Z`); `list_scratchpad` reports `geometry_type` the same way. Numeric arguments
(`size_m`, `height_m`, `extrude_m`, `base_z`) also accept strings such as `"20"`, `"20 m"` or `"1,5"`
(`_number`), because local models send those.

**`update_scratchpad_item`** (`update_item`) changes only the fields that are given; `TOOL_SCHEMAS`
lists it, so the cloud and ReAct backends offer it automatically. `label` (empty removes it, the map
then shows the name), `color` (`#rrggbb`), `alpha` (0..1) and `size_m` (merged into `data`) change only
the style. `class`, `clamp`, `height_m`, `extrude_m` and `base_z` go through `_shape()` (see
[2D and 3D](#2d-and-3d-heights-solids-boxes)), which may rewrite the stored geometry so that it
matches the class: "give the marker a height of 20 m" turns a `2Dmarker` (`POINT`) into a `3Dmarker`
(`POINT Z`), "make the polygon a 25 m solid" turns a `Polygon2D` into a `Solid3D`
(`POLYHEDRALSURFACE Z`). A featureset stays `FeatureSet` and cannot be raised or extruded (the error
suggests saving its 3D box instead). For other changes to the geometry or the objects, save again
under the same name. It returns `{ok, name, updated: {...}}`; `updated.geometry` is the new type
when the geometry was rewritten.

**Bulk-delete confirmation (simple version).** `scratchpad_action` with `delete` and **more than one**
name, and without `confirm: true`, deletes nothing and returns `{ok: false, needs_confirmation: true,
names, error}` with the instruction to ask the user. In the web UI, `scratch_tool_result` passes
`needs_confirmation` on and `scratch_tool_hint` (`llm_utils.py`) appends "do not call the tool again
now, ask the user, only after a yes call it again with confirm=true" to the tool result, in both the
cloud and the ReAct backend. Deleting one item, and the viewer's 🗑 button (one item),
need no confirmation. **Limitation:** this is an agreement between the model and the tool, not a
safeguard; the model could set `confirm=true` by itself. A confirmation button in the UI would be the
real protection (not done).

**Which user.** `effective_user_id()` takes the web UI user from `_ctx`. Without one (an external MCP
client such as Claude Desktop or VS Code over the HTTP endpoint) it uses `CITYDB_SCRATCHPAD_MCP_USER`,
or a fixed `uuid5` if unset. So **all external MCP clients share one scratchpad**. `run_query` uses the
same id, so those clients can query `my_scratchpad` too.

**Web UI wiring.**

- `app.py` builds `extra_tools` for every turn: the specs from `scratchpad_tool_specs()` plus a
  callable that makes the MCP call with the user's `_ctx`. They go through
  `backends/__init__.py` and `chat_runtime.stream_chat_turn` to the backends. The Agent activity panel
  shows the tool name and its JSON arguments. `scratch_tool_result` turns the tool's report into the
  `tool_result` event (preview rows, no highlight rows, the error if any).
- **Cloud backend** (`backends/cloud.py`): `all_tools` = the standard tools plus `extra_tools`, and
  calls are dispatched by `tc.function.name`. After a successful `run_query` the loop normally forces a
  text answer (`tool_choice="none"`). It does **not** when extra tools exist and the question has save
  or manage intent (`wants_scratchpad`), so "query, then save" works in one turn.
- **Local ReAct backend** (`backends/local.py`): each extra tool becomes a LangChain Tool whose Action
  Input is JSON (`_parse_json_input`). The parser's `tool_names` include them, the "unknown tool" error
  lists the available tools, native Ollama `tool_calls` for extra tools are accepted, and the callback
  emits `tool_call` / `tool_result` events with the tool name. The prompt's format line explains the
  JSON input.

### Reading saved items

An item's `sql` is a SELECT with an `objectid` column (featureset, a building set) or a `geom` column
(geometry, in the data's CRS; several rows are combined into one item). To list or reuse items, the
chatbot queries the view `my_scratchpad(name, label, kind, color, alpha, clamp, geom, objectids, is_visible, data,
created_at, updated_at, class)` (`class` is the last column). There is no other scratchpad table
visible to it.

```sql
-- 1. SQL for a building set (save_to_scratchpad, sql)
SELECT f.objectid
FROM feature f WHERE f.objectclass_id = 901 AND ST_ZMax(f.envelope) - ST_ZMin(f.envelope) > 30;

-- 2. SQL for a pin on top of a building
SELECT ST_SetSRID(ST_MakePoint(ST_X(ST_Centroid(f.envelope)), ST_Y(ST_Centroid(f.envelope)),
                               ST_ZMax(f.envelope)), ST_SRID(f.envelope)) AS geom
FROM feature f WHERE f.objectid = '<objectid>';

-- 3. SQL for a 3D convex hull around several buildings
SELECT CG_3DConvexHull(ST_Collect(f.envelope)) AS geom
FROM feature f WHERE f.objectid IN ('<id1>', '<id2>');

-- 4. reuse (run_query): buildings in set a but not in set b (set difference)
SELECT count(*) FROM (SELECT unnest(objectids) FROM my_scratchpad WHERE name = 'a'
                      EXCEPT SELECT unnest(objectids) FROM my_scratchpad WHERE name = 'b') d;
```

Hide, delete or fly to items with `scratchpad_action` (`hide`, `delete`, `goto`, one or several names).

**Why MCP tools.** The intent is explicit (a save is a tool call, not a side effect hidden in a SELECT),
the arguments are schema-validated, and the same tools work from Claude Desktop or VS Code without the
viewer. Both chat backends (cloud native tool calling and local ReAct) offer them next to the standard
tools.

Live comparison, same model (qwen3.8:27b-q8_0 on the Ollama server), once through the ReAct backend and
once through native tool calling over Ollama's OpenAI-compatible endpoint. Five questions in one chat:

| # | Question | Expected tool | Result |
|---|----------|---------------|--------|
| 1 | Save the Bräugasse buildings as `braeugasse` in green | `save_to_scratchpad` (`sql`) | 19 buildings saved |
| 2 | Save this pasted GeoJSON point as `treffpunkt` | `save_to_scratchpad` (`geojson`) | saved |
| 3 | Buildings within 100 m of `treffpunkt` | `run_query` on `my_scratchpad` | 67 |
| 4 | Hide `braeugasse` | `scratchpad_action` (`hide`) | hidden |
| 5 | List the scratchpad | `list_scratchpad` | listed |

Both backends chose the right tool 5 of 5 times. Native
tool calling also passed `class=2Dmarker` on its own. One cosmetic slip: an answer called the
default-orange pin "green".

**External MCP client test** (HTTP endpoint, no web UI session, so the shared MCP user):

| Check | Result |
|-------|--------|
| Tools listed | `save_to_scratchpad`, `update_scratchpad_item`, `scratchpad_action`, `list_scratchpad` (4) |
| GeoJSON FeatureCollection with 2 points, saved as `2Dmarker` | stored as MULTIPOINT |
| `run_query` join with `my_scratchpad` | 222 buildings within 100 m of either point |
| `Point3D` sphere with `size_m` | saved |
| Class mismatch (`Solid3D` for a point) | clear error |
| List and delete | work |
| `update_scratchpad_item`: a point to blue `Point3D` with `size_m` 4 | ok |
| Class change on a featureset | error "its class stays FeatureSet" |
| Delete of 2 items without `confirm` | `needs_confirmation`, nothing deleted |
| Delete of 2 items with `confirm: true` | both deleted |
| Audit file | `save`, `save`, `update`, `delete` lines |

## What happens on a save

1. The model calls `save_to_scratchpad`. The web UI sends the MCP call with the user's `_ctx` (the
   extra tool made in `_chat_turn` in `production/webui/app.py`), so the user id comes from the session
   layer, not from the model. `run_tool` builds the SQL (for `geojson` with `_geojson_sql`) and calls
   `scratchpad.save_item(user_id, name, sql, label, color, clamp, item_class, size_m, height_m,
   extrude_m, base_z)`.
2. `save_item` checks the user id (must be a uuid) and the name, takes the single SELECT/WITH
   statement (comments and a trailing `;` removed, `;` inside rejected) and runs it again,
   **read-only and as the user**: through the query role, with the user setting. It probes for one row
   first (no rows: error), then aggregates the whole result in the database. The save re-runs the
   SELECT without the `LIMIT 500` that `run_query` adds, so the saved count is the real one.
   For a geometry, `_GEOM_SQL` collects the rows (curves linearised); `_shape()` then makes it match
   its class (lift, extrude or flatten, see [2D and 3D](#2d-and-3d-heights-solids-boxes)) and
   `MAX_VERTICES` is checked before and after.
3. The result is stored through the **writer connection** (`INSERT ... ON CONFLICT (user_id, name) DO
   UPDATE`). The geometry goes from the read-only query as EWKB hex straight into the writer's insert;
   it never passes through the LLM or the MCP result.
4. The model gets the tool result `{ok, name, kind, count, type}` or `{ok: false, error}`;
   `scratch_tool_result` turns it into the `tool_result` event.
5. `note_scratch_result` reads the tool result and records which items the viewer should fly to (new
   items, `goto`).

## Data model

Table `<schema>.scratchpad` (default `citydb.scratchpad`), created by `provision()`:

| Column | Type | Notes |
|--------|------|-------|
| `user_id` | text | uuid from the session layer; primary key with `name` |
| `name` | text | CHECK `^[A-Za-z_][A-Za-z0-9_]{0,63}$` |
| `label` | text | shown on the map, defaults to the name in the viewer |
| `kind` | text | `featureset` or `geometry` |
| `class` | text | one of `CLASSES` (see "Classes"); `FeatureSet` for featuresets; CHECK `scratchpad_class_check` |
| `color`, `alpha` | text, double | `#rrggbb`; alpha 0..1, default 0.8 (`alpha` is set only by `update_scratchpad_item`) |
| `clamp` | boolean | drape 2D shapes on the terrain (a draped item is stored without heights) |
| `geom` | geometry | set for `geometry` items only (CHECK), GiST index (`gist_geometry_ops_nd`) |
| `objectids` | text[] | set for `featureset` items only (CHECK) |
| `is_visible` | boolean | show / hide |
| `data` | jsonb | `{"sql": <first 4000 chars>, "count": n}`, plus `size_m` when given (sphere / pipe radius) |
| `created_at`, `updated_at` | timestamptz | |

**Migration.** `provision()` also upgrades an existing table: `ALTER TABLE ... ADD COLUMN IF NOT EXISTS
class`, a backfill of existing rows (`_CLASS_MIGRATION`), the `scratchpad_class_check` constraint, and
the view `my_scratchpad` with `class` appended as its last column.

### Classes

`GEOMETRY_CLASSES` is the 15-class vocabulary of the original design
([idea-for-future-spatial-scratchpad.md](idea-for-future-spatial-scratchpad.md)); `CLASSES` adds
`FeatureSet`. `kind` (featureset / geometry) stays as well.

`2Dmarker`, `3Dmarker`, `Point2D`, `LineString2D`, `MultiLineString2D`, `Polygon2D`, `MultiPolygon2D`,
`Point3D`, `LineString3D`, `MultiLineString3D`, `Polygon3D`, `MultiPolygon3D`, `Surface3D`, `Solid3D`,
`MultiSolid3D`.

`classify()` derives the class from the geometry unless one is requested:

- points: `2Dmarker` (no Z) or `3Dmarker` (with Z);
- LineString / MultiLineString / Polygon / MultiPolygon: the `2D` or `3D` variant (`2D` if there is no Z
  or `clamp` is set);
- POLYHEDRALSURFACE / TIN: `Solid3D` if closed, else `Surface3D`;
- a collection of closed polyhedral surfaces: `MultiSolid3D`; a collection of polygons:
  `MultiPolygon2D` / `MultiPolygon3D`; mixed collections are rejected.

A **requested** class (the `class` argument) is validated against the geometry
type (`_CLASS_TYPES`); a mismatch, e.g. `Solid3D` for a point, is an error with the allowed classes.
`classify()` also refuses a 3D class for a geometry without heights; `_shape()` lifts the geometry
before that check, so through the tools the class and the stored dimension always agree.

### 2D and 3D: heights, solids, boxes

Before this rule, `update_scratchpad_item` with `class: 3Dmarker` or `class: Polygon3D` only changed
the `class` column: the table still held a `POINT` / `POLYGON` without heights, and the viewer kept
drawing it flat. Now the **class and the stored geometry always agree** (`FLAT_CLASSES` are the six
`…2D` / `2Dmarker` classes; every other class is 3D):

| Request | What `_shape()` does to the stored geometry | Class afterwards |
|---|---|---|
| `height_m: 20` | Lifts the item so its lowest point is 20 m above the ground (`ST_Force3DZ` + `ST_Translate`). A 3D item is moved to that height. Negative values (below ground) are allowed. | the 3D variant: `2Dmarker` -> `3Dmarker`, `Polygon2D` -> `Polygon3D`, ... |
| `extrude_m: 25` | Lifts a 2D area/line onto the ground, then `CG_Extrude(g, 0, 0, 25)` (SFCGAL). A 3D area is extruded from its own height. | area -> `Solid3D` (closed), multi-area -> `MultiSolid3D`, line -> `Surface3D` (a wall). Points cannot be extruded. |
| `base_z: 350` | Like `height_m`, but measured from this absolute height (m, the data's height system, e.g. NHN) instead of the ground. Both together: `base_z + height_m`. | 3D variant |
| a 3D `class` on a 2D item, e.g. `3Dmarker` | Lifts it onto the ground (height 0). A `Point3D` sphere is lifted by its radius, so it rests on the ground. | the requested class |
| `class: Solid3D` on an area without `extrude_m` | Nothing; error "an area becomes a Solid3D with extrude_m". | unchanged |
| a 2D `class` on a 3D item, or `clamp: true` | Removes the heights (`ST_Force2D`). This cannot be undone with `clamp: false`. | the 2D variant |

**Ground height.** The database has no terrain model (no relief features), so the ground under an
item is estimated as the lowest point (`ST_ZMin` of the envelope) of the **3 nearest city objects
within 500 m** (`_GROUND_SQL`, `GROUND_RADIUS_M`). Without any object nearby the tool asks for
`base_z`. For the Passau data this is within about a metre of the 3D tiles' ground; on steep slopes
it errs low (the lowest of the three), so a solid rather stands slightly in the ground than floats.

**Bounding boxes.** The model used `ST_MakeEnvelope` with six coordinates for a "3D bounding box";
that function is 2D only. A 3D box is `ST_3DExtent(...)`, which returns a `box3d`. The tool's
description and the prompt block now say so:

```sql
-- 3D box around a saved set (closed box, Solid3D)
SELECT ST_3DExtent(f.envelope) AS geom FROM feature f
JOIN my_scratchpad p ON f.objectid = ANY(p.objectids) WHERE p.name = 'selected'
-- 2D box (Polygon2D)
SELECT ST_Extent(f.envelope) AS geom FROM feature f WHERE ...
```

**Which PostgreSQL / PostGIS types can be saved.** The `geom` column of the SELECT is cast with
`::geometry`, so any type with a cast to `geometry` works. Checked live (PostGIS 3.6.4 + SFCGAL):

| Type in the SELECT | Typical source | Stored as | Class |
|---|---|---|---|
| `geometry` POINT / MULTIPOINT (Z) | `ST_MakePoint`, GeoJSON | as is | `2Dmarker` / `3Dmarker` (`Point2D` / `Point3D` on request) |
| `geometry` LINESTRING / MULTILINESTRING (Z) | `ST_MakeLine` | as is | `LineString2D/3D`, `MultiLineString2D/3D` |
| `geometry` POLYGON / MULTIPOLYGON (Z) | `ST_Buffer`, `ST_MakeEnvelope` | as is | `Polygon2D/3D`, `MultiPolygon2D/3D` |
| `geometry` POLYHEDRALSURFACE Z / TIN / TRIANGLE | 3DCityDB solids, `CG_Extrude`, `CG_3DConvexHull`, `CG_MakeSolid` | as is | `Solid3D` if closed, else `Surface3D` |
| `geometry` GEOMETRYCOLLECTION of polyhedral surfaces | `CG_Extrude` of a multipolygon, `ST_Collect` of solids | as is | `MultiSolid3D` (closed) / `Surface3D` |
| `box3d` | `ST_3DExtent`, `ST_3DMakeBox` | closed `POLYHEDRALSURFACE Z` (6 faces); a flat box becomes a `POLYGON Z` | `Solid3D` / `Polygon3D` |
| `box2d` | `ST_Extent`, `ST_MakeBox2D` | `POLYGON` (gets the dataset SRID) | `Polygon2D` |
| `geography` | `ST_Buffer(geography, ...)` | `geometry` in EPSG:4326, then transformed to the dataset CRS | as its geometry type |
| curves: CIRCULARSTRING, COMPOUNDCURVE, CURVEPOLYGON, MULTISURFACE | `ST_CurveToLine` input, imported arcs | linearised with `ST_CurveToLine` (`ST_HasArc`) | the line / polygon class |
| `text` WKT / EWKT | `'POINT(...)'` | parsed by the cast | as its geometry type |

Not storable: collections mixing points with lines or areas (save the parts separately), and
non-geometry types (raster, topology).

Rules (constants in `scratchpad.py`):

- Saving an existing name **replaces** it (upsert; the item becomes visible again). Names are unique
  per user, not globally.
- `MAX_ITEMS = 100` per user (replacing an existing name is still allowed when full).
- `MAX_VERTICES = 20,000` per geometry item (`ST_NPoints` after collecting). Larger ones are rejected
  with the hint to simplify first (`ST_Simplify`, `CG_3DConvexHull`).
- `MAX_FEATURESET = 100,000` distinct objectids.
- Several `geom` rows are combined with `ST_Collect`; one row is used as is.
- SRID: geometries with SRID 0 get the dataset's SRID (`database_srs`), other SRIDs are transformed to
  it. The viewer payload converts to WGS84 (GeoJSON, 7 decimals) and keeps the original heights.

## Isolation between users

The app connects to the database as a superuser, and superusers bypass row-level security, so RLS
(the original idea) would protect nothing. The isolation is by **database permissions** instead:

- **Query role** `citydb_query` (`CITYDB_QUERY_USER`), created by `provision()`: `LOGIN NOSUPERUSER
  NOCREATEDB NOCREATEROLE`, `default_transaction_read_only = on`, `SELECT` on the whole city model
  schema (including future tables), and **no access to the table** `scratchpad` (explicit `REVOKE`).
  Its only way to the scratchpad is the view `my_scratchpad`.
- **View** `my_scratchpad` is `WITH (security_barrier)` and filters
  `user_id = current_setting('citydb.user_id', true)`.
- **Per-call user setting.** The user id comes from `_ctx` (the session layer, "Phase 0"), not from
  the model. `call_tool` in `src/citydb_mcp/server.py` passes
  `scratchpad.user_settings(current_tool_ctx().get("user_id"))` to `run_query`, and
  `DatabaseConnection.execute(settings=...)` (`src/citydb_mcp/db.py`) runs
  `set_config(name, value, true)` in the same transaction, then commits. `is_local = true` means the
  value cannot leak to the next user of a pooled connection. Without a valid uuid, no setting is
  made, `current_setting(..., true)` is NULL and the view returns **0 rows**.
- **Writer connection** (`DatabaseConnection(read_only=False)`) is used only by `save_item`,
  `apply_action`, `touch_user`, `delete_user` and `provision`, and always with `WHERE user_id = %s`.
  Every other connection stays `default_transaction_read_only = on`.
- **run_query_target** picks the query role for `run_query` when the scratchpad is on. If the role
  cannot log in (not provisioned, BYOD), it logs a warning and falls back to the owner connection
  **with `guard_sql`**, a regex that refuses `scratchpad`, `set_config`, `current_setting` and
  `pg_settings`. That guard is only a best-effort string check, not a boundary; with the role in place
  the database itself refuses. `save_item` applies the same guard to the SQL it re-runs.
- **Geometry shortening.** `run_query` replaces EWKB hex values (strings that look like geometries)
  with `<geometry POLYGON Z, 1.2 kB>` (`_shorten_geometries` / `_describe_ewkb` in
  `runtime_tools.py`), so a `SELECT geom FROM my_scratchpad` does not flood the model's context.

Verified live (two users, fullstack setup):

| Check | Result |
|-------|--------|
| User B lists `my_scratchpad` after user A saved items | 0 of A's rows |
| `run_query` with no user id | 0 rows |
| `SELECT ... FROM citydb.scratchpad` as the query role (also without the code guard) | permission denied |
| `query_to_xml('select ... from citydb.scr'\|\|'atchpad')` (evades the regex) | permission denied |
| Write statements as the query role | error: read-only transaction |
| User B deletes A's item by name (`scratchpad_action`) | not found, A's item stays |

Residual risks:

- The user id is a **bearer key** (see the session layer). Whoever knows another user's uuid can read
  that user's items, e.g. with `set_config('citydb.user_id', '<uuid>', true)` inside dynamic SQL.
  uuids cannot be listed (the query role cannot see the table), so this needs the uuid itself. Real
  per-user separation needs a login in front, as noted in the session layer.
- The app still connects as a superuser for everything except `run_query` and the scratchpad writer
  (schema tools, viewer lookups, the viewer payload).
- In the guard fallback (no query role) the isolation is only as good as the regex.

## Viewer

The viewer mirrors the table. The web UI sends the **complete list** of the user's items, and the
viewer replaces its scratchpad layer with it.

- **After every turn.** `_chat_turn` builds `build_scratchpad_payload(user_id, focus)` and attaches it
  to the highlight payload as `scratchpad` (like `flood`); the JS in `app.py` forwards it with
  `window._sendScratchpad`. After a save, the **orange highlight of the preview rows is dropped**
  (`buildings` emptied), because the item shows the full result.
- **On page load.** `_on_register_user` (the `demo.load` handler) returns the payload into the hidden
  textbox `#scratch-state`; its `change` event calls `window._sendScratchpad(JSON.parse(raw))`. The
  value is a **JSON string in a textbox**: a `gr.JSON` output of a load event arrived in the browser
  as a Python repr string (`root={...}`). A `t` nonce makes a repeated identical payload still a
  change.
- **Handshake.** `scratchpad-bridge.js` posts `scratchpad-ready` to the parent when it loads. The
  parent keeps the last payload in `window.__citydbScratchpad` and re-sends it, so a viewer that loads
  after the data still gets it. The bridge also retries every 500 ms until Cesium and the tileset exist.

**Rendering** (`production/webmap-overlay/scratchpad-bridge.js`, script tag in
`production/docker/Dockerfile`):

- *featureset*: buildings coloured per feature, like the flood scene. Tile features are boundary
  surfaces, mapped to their building by objectid prefix. Priority: **selection (purple) > featureset
  colour**; unlisted buildings are light grey. **While an answer's highlight (orange) is shown, the
  sets pause** (an orange highlight on an orange or red set would not stand out) and come back with
  the next answer without highlight; `release()` removes their per-feature colours before the
  highlight style takes over. A small hook in `highlight-bridge.js` (`applyCombinedStyle` ->
  `window._citydbScratchpad`) hands the colouring over. While a **flood scene is active**,
  `flood-bridge.js` colours the buildings and draws the visible sets on top in their own colours
  (`window._citydbScratchpad.colorFor`), again not while a highlight is shown; the flood keeps its
  full red/green around them. Order there: selection > highlight > focus buildings > featureset > flood.
- *geometry*: Cesium entities in their own data source, at the data's **raw heights** (the same
  convention as tiles and the flood water). The drawing follows the `class`:
  markers (`2Dmarker`, `3Dmarker`): pin with a label; `Point3D`: ellipsoid sphere with radius `size_m` (centred at the point's height; a point without height, e.g. from GeoJSON, gets a sphere resting on the terrain)
  (default 1 m); `Point2D`: a plain dot on the ground; `LineString3D` / `MultiLineString3D` with Z:
  a `polylineVolume` pipe with radius `size_m` (default 0.5 m); other lines: polylines. A floating
  `3Dmarker` also gets a thin vertical line from the pin down to the terrain (`addStem`; the terrain
  height is sampled once with `sampleTerrainMostDetailed`, 0 without terrain). Polygons and
  solid faces: filled polygons with `alpha`. `clamp` or a missing Z drapes the shape on the
  terrain. Other geometries get a label above their bounding box.
- *camera*: items that are new in this turn, and `goto`, fly to the items' combined bounding box
  (1.5 s, pitch -35 degrees).
- *item list* (bottom right, English or German by browser language): colour swatch, name (click flies
  to it; the tooltip shows the class), eye (show/hide) and bin (delete). The header ("Meine Objekte
  (50)") stays fixed and only the list scrolls, at most 220 px high (`LIST_MAX_PX`, less in a small
  viewer: `45vh - 40px`), so many items never cover the map. A refresh keeps the scroll position, a
  new or flown-to item is scrolled into view, and a click on the header collapses the list (remembered
  per browser in `localStorage`, `citydb_scratchpad_collapsed`). Checked with 50 items in a 540 px high
  viewer: the list is 203 px high and scrolls through 1,250 px of rows.

**List round trip.** A click posts `scratchpad-action` to the parent page. The parent accepts it only
from the viewer iframe on the same origin and writes `{action, name, t}` into the hidden textbox
`#scratch-sink`. Its change event runs `_on_scratch_sink`, which treats the value as untrusted browser
input: it must parse as a JSON object, `action` must be in `ACTIONS` and `name` must pass
`valid_name`. It then runs `apply_action` **for this tab's user only** (`user_for(session_hash)`) and
returns a fresh payload into `#scratch-state`, with `show` and `goto` as focus.

## Audit log

Every change is appended as **one JSON line** to a JSONL file, so "what did the model change or remove"
can be traced afterwards: `save`, `update`, `show`, `hide`, `delete` and `purge` (the expiry hook).
`goto` changes nothing and is not logged. Fields: `ts` (UTC), `op`, `user_id`, `name` or `names`, plus
details (a save: `kind`, `class`, `count`, `geometry_type`; an update: `changed` with the new values).

```json
{"ts": "2026-10-30T10:52:47+00:00", "op": "update", "user_id": "3b15b460-84db-59a8-b80c-87ebd25caddf", "name": "a", "changed": {"color": "#2563eb", "class": "Point3D", "size_m": 4.0}}
```

- Path: `CITYDB_MCP_AUDIT_LOG`, default `/app/data/audit/scratchpad.jsonl` when `/app/data` exists, which
  is `production/data/audit/` on the host through the mounted volume. Set it empty to switch the log
  off. The directory is created on the first write.
- Both the MCP server and the web UI write to it (`_audit` in `scratchpad.py`, append, one line per
  write). A failed write is printed to the log and never fails the operation.

## Expiry

- `_start_scratchpad` registers `scratchpad.delete_user` as a **purge hook** of the session layer, so
  when an inactive user is purged (`CITYDB_USER_EXPIRY_DAYS`, default 14), their rows go too.
- `touch_user` sets `updated_at = now()` on all of the user's items once per chat turn. Which users
  are inactive is still decided by the session layer's `.last_seen` file.

## CLI and benchmark

`3dcitydb-chat` (`production/webui/cli.py`) offers the same four tools as the web UI when
`CITYDB_SCRATCHPAD=true` (before, its prompt described tools it did not pass to the backend).

- `ChatConsole` builds `extra_tools` from `scratchpad_tool_specs()` and sends a `_ctx`
  (`user_id`, `session_id`, `turn_id`) with `run_query` and every scratchpad call, so
  `my_scratchpad` works in its queries as well.
- Each console gets its **own generated user id**, so every chat run and every benchmark
  conversation (`webui/benchmark.py` creates one console per conversation) has an isolated,
  empty scratchpad. `ChatConsole.close()` deletes that user's items at the end of the run or
  conversation (the benchmark calls it in a `finally`, also after an interrupted turn); the
  audit log records it as `purge`.
- `--scratchpad-user <uuid>` uses an existing scratchpad and keeps it. With the web UI's id
  (`localStorage.citydb_user_id` in the browser) items saved in the terminal show up in the viewer
  after a reload.
- The startup line on stderr shows the user id and whether its items are kept.
- Scratchpad reports are not reused as the CLI's tool cache (only `run_query` results are).

```bash
docker exec -it production-citydb-agent-1 3dcitydb-chat chat --provider ollama --model qwen3.8:27b-q8_0
# into the browser's scratchpad:
docker exec -it production-citydb-agent-1 3dcitydb-chat chat --scratchpad-user <citydb_user_id>
```

There is no viewer in the terminal: `goto` and the colours have no effect there.

## Limitations

- The ground height for `height_m` / `extrude_m` is an estimate from nearby city objects, not a
  terrain model (see [2D and 3D](#2d-and-3d-heights-solids-boxes)); give `base_z` where it matters.
- Flattening (a 2D class or `clamp: true` on a 3D item) drops the heights for good.
- During a flood scene a set in the flood's own red (`#dc2626`) cannot be told apart from the
  flooded buildings; pick another colour for sets you want to see over a flood, or hide the set
  with 👁 to see the flood alone.
- Very large geometries are rejected (more than 20,000 vertices): simplify or take a hull first.
- Colouring needs the tileset to use the building objectid as prefix of its surface ids; BuildingParts
  may be coloured incompletely, as in the flood scene.
- The item list belongs to the user id, which lives in the browser profile's `localStorage` (same as the
  session layer): a private window or another browser has an empty list.
- Headless or night-time rendering darkens the colours, because Cesium follows the real clock.
- All external MCP clients share one scratchpad (`CITYDB_SCRATCHPAD_MCP_USER`); there is no separation
  between them.
- The bulk-delete confirmation is only an agreement between the model and the tool (see "MCP tools"):
  a UI confirmation button would be the real safeguard. The audit log is a plain file without rotation.

## Testing

`tests/test_scratchpad.py` uses a fake database (the isolation itself is checked live, below). It covers:

- `save_item`: re-running the SQL as the user (settings on every query), featureset and geometry
  storage, rejection of bad names, non-SELECT, multiple statements and guarded SQL, missing user,
  missing columns, no rows, `MAX_VERTICES` and `MAX_ITEMS`.
- `apply_action`: found/missing names, duplicates and invalid names dropped, unknown action;
  `delete_user` and `touch_user` only for valid uuids.
- `guard_sql` and `user_settings`; `run_query` passing settings, applying the guard and shortening
  EWKB values (`_describe_ewkb`, including ISO Z types and garbage).
- `classify()`: derived classes and validation of a requested class.
- `update_item` (fields, class validation, featureset stays `FeatureSet`), the bulk-delete
  confirmation and the audit lines.
- 2D/3D (`_shape`, with a fake that answers the shape query according to the expression):
  `height_m` turns a `2Dmarker` into a `3Dmarker` (lift to ground + height, the new EWKB stored),
  a 3D class alone lifts to the ground and a `Point3D` by its radius, a 2D class flattens, an area
  needs `extrude_m` for `Solid3D`, a line becomes a `Surface3D` wall, points cannot be extruded,
  `base_z` skips the ground estimate, no ground -> "give base_z", featuresets refuse heights;
  `_number` parsing and the new tool properties.
- `tests/test_chat_cli.py`: the CLI passes the four tools and its own user in `_ctx`, two consoles
  get different users, scratchpad reports do not become the tool cache, `close()` removes a
  generated scratchpad but keeps a given one, `--scratchpad-user` is validated.
- Web UI side: `scratch_tool_result` / `scratch_tool_hint`, `note_scratch_result` (focus names from the
  tool result), the prompt block (`SCRATCHPAD_TOOLS_INSTRUCTIONS`) only when enabled and no
  `scratch_save` convention in it, and the shape of `list_items`.

```
pytest tests/test_scratchpad.py -v
```

Live checks done:

- **Two users, isolation**: the table of the checks above.
- **Conversation with a local model (qwen3.8)**: saved `betroffen_2013` (1,425 buildings) and
  `betroffen_ms4` (114), computed the difference (1,311), saved a pin "Bräugasse 20", hid an item and
  listed the items. A second user saw nothing.
- **Headless Chrome**: after a reload, the viewer restores 3 items (featureset with 1,586 buildings
  coloured, the hull with 54 faces, the pin); a save in the chat updates the viewer; a click on the eye
  hides the item through the server.
- **Update, bulk-delete confirmation, both backends** (qwen3.8:27b-q8_0, once through ReAct and once
  through native tool calling over Ollama's OpenAI-compatible endpoint): save `braeugasse` (19
  buildings), save the GeoJSON `treffpunkt`, "Färbe treffpunkt blau" -> `update_scratchpad_item` with
  `color=#0000ff` (both). "Lösche braeugasse und treffpunkt" -> both models asked for confirmation
  without calling a tool and the items were still there. "Ja, lösche beide" -> `scratchpad_action`
  `delete` with `confirm=true`, both items deleted (both backends).
- **Headless Chrome, 3D classes**: a `Point3D` sphere (radius 6 m), a `3Dmarker` floating at 345 m with
  its line down to the ground, and a `LineString3D` pipe (radius 2 m) are all drawn at their heights,
  with labels.
- **2D -> 3D against PostGIS** (all through `run_tool`): GeoJSON point -> `height_m: 20` ->
  `3Dmarker`, `POINT Z` at ground (295.3 m) + 20; `class: 2Dmarker` -> `POINT` again; the
  `ST_MakeEnvelope` box of 3 buildings -> `class: Solid3D` refused ("extrude_m"), `Polygon3D`
  (`POLYGON Z` at 309.98 m), `extrude_m: "25 m"` -> `Solid3D` `POLYHEDRALSURFACE Z`; `ST_3DExtent` ->
  `Solid3D` directly (309.36 to 336.8 m); `ST_Extent` -> `Polygon2D`; a line with `extrude_m` -> wall
  `Surface3D`; a `CIRCULARSTRING` -> linearised `LineString2D`; `base_z: 350` -> `POINT Z` at 350.
- **CLI with qwen3.8:27b-q8_0 (ReAct)**, five questions in one run: 2D marker from GeoJSON ->
  "Gib dem treffpunkt eine Höhe von 20 m" (`update_scratchpad_item` `height_m: 20`) -> 2D bounding box
  of 3 buildings (`ST_Extent`) -> "Mach aus bbox_sel einen 3D-Körper, 25 m hoch" (`extrude_m: 25`) ->
  the list showed `3Dmarker` / `POINT Z` and `Solid3D` / `POLYHEDRALSURFACE Z`; at exit "removed 2
  item(s) of this run".
- **Headless Chrome**: the extruded box is drawn as a translucent solid with its vertical walls
  around the coloured buildings, the 3D marker floats above the roofs with its line to the ground.
