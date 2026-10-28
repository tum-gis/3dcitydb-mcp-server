# Idea: Spatial Scratchpad — a temporary, visualizable results table in the 3DCityDB

## Status: draft, not yet implemented

A proposed new table plus a small set of MCP operations that let the agent
**create, store, style, navigate to, visualize, and delete** lightweight
spatial objects ("scratchpad" items) that render directly in the 3D viewer.

This is an additive, low-risk feature: it never touches the imported CityGML
model, only rows in one dedicated `citydb.scratchpad` table, isolated per user
via **row-level security** (see
[Isolation & security model](#isolation--security-model)). The only DDL in the
entire feature is a one-time bootstrap (table, index, policy, one purge
function); at runtime, users need **only DML on one table** and no DDL at all.

---

## Motivation

During analysis the agent often produces **intermediate** or **derived**
spatial results that a user wants to *see* in the 3D viewer, not just read as
rows in a chat:

- **Spatial markers / pinpoints** — "drop a labeled pin at this address,"
  "mark where this pipe crosses the road."
- **Grouped / derived solids** — "take these 40 buildings and alpha-wrap
  them; save the resulting 3D solid as *'Flood cluster 3'*, colored red."
- **2D thematic overlays** — "show the district boundary as a polygon draped
  over the terrain, semi-transparent."

Today the only things the viewer displays are the CityGML features from the
tileset, plus transient highlight/selection styling (see
[highlight-bridge.js](../webmap-overlay/highlight-bridge.js)). There is no way
to persist a *temporary* geometric result and have the viewer render it with a
name, a color, and a label. The scratchpad fills that gap.

## Design goals

1. **Separate table** — one `citydb.scratchpad` table, fully independent of
   the imported CityGML schema, so re-importing GML never wipes the user's
   scratch items (and vice versa).
2. **Mixed 2D + 3D, in two columns** — each row stores its geometry in a
   dedicated 2D *or* 3D column (chosen by `class`), each with its own spatial
   index.
3. **Viewer-ready** — every row carries everything the viewer needs to render
   it: `name` (handle), `label` (on-map text, optional), `color`,
   `transparency`, and the geometry. No join
   against CityGML to display.
4. **Arbitrary thematic payload** — a `JSONB` column for any extra attributes
   (counts, units, source query, derivation timestamp, …).
5. **Per-user isolation enforced by the database** — each user's scratch items
   are invisible to every other user *even through raw SQL* (via row-level
   security or per-user schemas; see [Isolation & security model](#isolation--security-model)),
   not merely by a filter the tool layer applies.
6. **Name-based handles** — every item is addressed by a user-chosen, per-user
   unique **identifier `name`** (a spatial variable, e.g.
   `my_nice_result2`; no spaces, any keyword-matching identifier allowed);
   there is no separate numeric id. A separate nullable
   **`label`** carries the human-readable on-map text (`COALESCE(label, name)`).
7. **MCP round-trip** — dedicated tools to create / list / navigate / show /
   hide / delete scratchpad items, so the whole lifecycle is driven from chat.

## The `class` vocabulary and where the geometry lives

The `class` column is a **discriminator**: it tells the viewer *how* to render
the row and which geometry column is authoritative.

| Class | Dimension | Geometry column | Rendering |
|-------|:---------:|-----------------|-----------|
| `2Dmarker`, `3Dmarker` | pin | `geometry2D` (2D) **or** `geometry3D` (3D) | A pin/point marker **connected to the ground** (`2Dmarker`) or floating at a 3D coordinate (`3Dmarker`), with a **text label** (`COALESCE(label, name)`) |
| `Point2D`, `LineString2D`, `MultiLineString2D`, `Polygon2D`, `MultiPolygon2D` | 2 | `geometry2D` | Rendered **draped over the terrain** (clamped to ground) |
| `Point3D`, `LineString3D`, `MultiLineString3D`, `Polygon3D`, `MultiPolygon3D`, `Surface3D`, `Solid3D`, `MultiSolid3D` | 3 | `geometry3D` | Rendered as **floating 3D geometry** at its own height (not clamped) |

Conventions:

- A row stores its geometry **only** in the column matching its dimension; the
  other column is `NULL` (enforced by a `CHECK`, see below).
- Markers reuse the same two columns; the `marker` in the `class` value is what
  switches the viewer from "draw the geometry" to "draw a pin + label at this
  location." A `3Dmarker` may store a true 3D point (so the pin can float above
  ground); a `2Dmarker` stores a 2D point and is clamped/dropped to the terrain.
- The geometry `SRID` is **the same as the 3DCityDB's working CRS** — the value
  from `SELECT srid FROM database_srs LIMIT 1`, the same source the server
  already reads in [server.py](../../src/citydb_mcp/server.py) and inspects in
  [dynamic_tools.py](../../src/citydb_mcp/tools/dynamic_tools.py) to tell 2D vs
  3D / geographic vs projected. The viewer bridge transforms to WGS84 (EPSG:4326)
  for display, exactly as [highlight.py](../../src/citydb_mcp/tools/highlight.py)
  already does.

## Proposed table schema

**Two geometry columns** are used deliberately (not one): it makes the
dimension contract self-documenting, lets us constrain "exactly one populated,"
and — importantly — lets us put a **2D spatial index on `geometry2D`** and a
**3D spatial index on `geometry3D`** independently.

The primary key is the **user-chosen `name`** (see below). There is no
`SERIAL`/`item_id` column: if names are unique per user and immutable, the name
*is* the handle, and a numeric id would be dead weight.

```sql
CREATE TABLE IF NOT EXISTS citydb.scratchpad (
    -- owner: set per-connection via current_setting('app.current_user_id').
    -- Never written by tool arguments; the DB kernel binds it via RLS.
    user_id       TEXT NOT NULL,

    -- the handle. An identifier (no spaces/special chars), per-user unique,
    -- immutable. This is a variable name (e.g. my_nice_result2), NOT
    -- necessarily a friendly label. Any valid identifier is allowed, even one
    -- that matches a SQL keyword — it's always passed as a parameter/quoted.
    name          TEXT NOT NULL,

    -- optional human-readable label shown on the map. Falls back to `name`
    -- when NULL. Free prose ("Flood cluster 3 — red") is allowed here.
    label         TEXT,

    -- display + classification
    class         TEXT NOT NULL
        CHECK (class IN (
            '2Dmarker','3Dmarker',
            'Point2D','LineString2D','MultiLineString2D','Polygon2D','MultiPolygon2D',
            'Point3D','LineString3D','MultiLineString3D',
            'Polygon3D','MultiPolygon3D','Surface3D','Solid3D','MultiSolid3D'
        )),

    -- styling (per-item, viewer-consumed)
    color         TEXT NOT NULL DEFAULT '#ff5a00',   -- CSS hex, e.g. '#ff5a00'
    transparency  DOUBLE PRECISION NOT NULL DEFAULT 1.0
        CHECK (transparency BETWEEN 0.0 AND 1.0),   -- 1.0 = opaque

    -- geometry (exactly one populated, per `class`); both SRID = database_srs.srid
    geometry2D    GEOMETRY,
    geometry3D    GEOMETRY,

    -- viewer visibility (also mirrored as a Cesium entity show flag).
    is_visible    BOOLEAN NOT NULL DEFAULT true,

    -- arbitrary additional thematic data
    data          JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- bookkeeping
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- per-user uniqueness: the handle. (Composite because user_id is NOT NULL.)
    PRIMARY KEY (user_id, name)
);

-- 2D spatial index (queries on geometry2D should be Z-agnostic, e.g. ST_DWithin on a 2D geom)
CREATE INDEX IF NOT EXISTS scratchpad_geom2d_idx
    ON citydb.scratchpad USING GIST (geometry2D);

-- 3D spatial index (includes the Z dimension for true 3D containment/proximity)
CREATE INDEX IF NOT EXISTS scratchpad_geom3d_idx
    ON citydb.scratchpad USING GIST (geometry3D);

-- exactly one geometry column is populated
ALTER TABLE citydb.scratchpad ADD CONSTRAINT scratchpad_geom_one_notnull
    CHECK (
        (geometry2D IS NOT NULL AND geometry3D IS NULL)
        OR (geometry2D IS NULL AND geometry3D IS NOT NULL)
    );
```

Notes / decisions already made:

- **Two columns + two GiST indexes** — settled. A single `GEOMETRY` column
  would have been simpler but loses the independent 2D/3D index control.
- **`name` is the handle; `label` is the on-map text.** `name` is a variable:
  an **identifier** (tool-layer enforced: `^[A-Za-z_][A-Za-z0-9_]{0,63}$` — no
  spaces or special characters, starts with a letter/underscore), per-user
  unique (the composite PK enforces this at the DB kernel, not just the tool
  layer), and **immutable** — "rename" is delete + create. No reserved-word
  list: any valid identifier may be a name, even one that coincides with a SQL
  keyword (`where`, `select`, …). The keyword case is harmless because scratchpad
  names are **never inlined raw into SQL** — the tools pass them as bound
  parameters (`WHERE name = %s`), and any agent-composed SQL must emit them
  double-quoted (`WHERE name = "where"`); both are covered by the query
  guidelines. The nullable `label` is the human-readable text rendered in the
  viewer; the viewer shows `COALESCE(label, name)` so an item is always
  labeled even when `label` is omitted. This separation matters because a good
  *variable name* (`my_nice_result2`) is usually not a good *map label*
  ("Flood cluster 3 — red"), and only `label` may be free prose.
- **No numeric id.** With `name` unique per user and immutable, it's a safe
  primary key. The only thing dropped is cheap in-place renames, which we don't
  need for v1. (If a future feature needs a surrogate for, e.g., join stability
  across a rename, add `id BIGSERIAL` then — it costs nothing to add later.)
- **`GEOMETRY` (untyped) vs typed.** Untyped keeps the table uniform across all
  classes; the *tool layer* validates type-vs-`class` on write (a `Polygon2D`
  row can't receive a `Solid3D`) and 2D-vs-3D dimensionality
  (`ST_CoordinateDimension`). Because the working CRS is *data* (from
  `database_srs`), not a compile-time constant, tool-side validation plus a
  `CHECK (ST_SRID(...) = <srid> OR NULL)` trigger is the cleanest SRID
  enforcement.

## MCP server operations

New tools, registered alongside the existing ones in
[server.py](../../src/citydb_mcp/server.py) (`list_tools` + `_execute_tool`).
Each is a thin, parameterized wrapper around the `scratchpad` table — the
"structured-function" style recommended at the end of
[idea-for-future-write-access.md](idea-for-future-write-access.md), which is
deliberately *narrower* and *safer* than arbitrary DML.

**Ownership is injected, not supplied.** Every scratchpad tool runs inside a
transaction that first sets the connection's identity —
`SET LOCAL app.current_user_id = '<uid>'` — from the server-side session
context ([runtime_tools.py](../../src/citydb_mcp/tools/runtime_tools.py)
`_sessions`), keyed by the request's `user_id` (see
[Isolation & security model](#isolation--security-model)). The DB kernel then
binds that identity to the row-level security policy (or, in the per-user-schema
variant, to the schema search path). The LLM never passes `user_id` in a tool
argument and **cannot filter it away in raw SQL**, because the policy is
enforced below the query layer.

**Items are addressed by `name`** — the user-chosen variable name. In chat
this reads naturally: *"store the resulting geoobject in `my_nice_result2`"*
→ a row named `my_nice_result2`.

| Tool | Args (key ones) | What it does |
|------|-----------------|--------------|
| `create_scratchpad_item` | `name`, `class`, `wkt`, `label?`, `color?`, `transparency?`, `data?` | Validate `name` is a legal identifier (`^[A-Za-z_][A-Za-z0-9_]{0,63}$`; any keyword-matching identifier is allowed) and per-user unique (→ clear "already exists" error), parse WKT, pin SRID to the DB CRS, route to `geometry2D`/`geometry3D` by `class`, validate type-vs-class, insert. `label` is optional free text (defaults to `name` for display) |
| `update_scratchpad_item` | `name`, any of `label/color/transparency/wkt/data` | Partial update — **`name` itself is immutable** (rename = delete + create), so the `name` arg only selects the row; `label` *is* editable in place |
| `list_scratchpad_items` | `class?`, `limit?` | Lightweight summary rows (`name, label, class, color, is_visible, geomtype, bbox`) |
| `get_scratchpad_item` | `name` | Full detail incl. WKT + `data` |
| `navigate_to_scratchpad_items` | `names: [name, …]` | Compute **one** camera view (bounding sphere) that contains *all* named items, and emit the fly-to. A single item is passed as a one-element list. Returns which names were not found |
| `show_scratchpad_items` | `names: [name, …]` | Make the named items visible in the viewer (and DB) |
| `hide_scratchpad_items` | `names: [name, …]` | Hide the named items (viewer + DB flag) |
| `delete_scratchpad_items` | `names: [name, …]`, `confirm?` | Delete the named rows. Bulk deletes (many names) require `confirm: true`; a single-element list does not |

All four viewer-affecting tools (`navigate_…`, `show_…`, `hide_…`, `delete_…`)
take a **list of names**, never a bare name — so "hide the three flood
clusters" is one call, and `navigate_to_scratchpad_items(["a","b","c"])`
frames *all three* in a single camera move (union of their extents → one
`BoundingSphere` → one `flyToBoundingSphere`, exactly the `flyToSphere`
pattern the highlight bridge already uses for multi-feature targets). Missing
names are reported per call rather than aborting the whole batch (a `delete`
of a typo'd name still deletes the ones that exist).

Design choices:

- **WKT in, SRID pinned server-side.** The model supplies geometry as WKT
  (matching the `replace_geometry` convention already specced in the
  write-access doc); the tool sets the SRID to `database_srs.srid`, so the
  model never has to know the CRS (consistent with the "never assume the CRS"
  server guideline in [server.py](../../src/citydb_mcp/server.py)).
- **`class`-validated geometry.** On write, the tool checks the parsed geometry
  type against the `class` (a `Polygon2D` row can't receive a `Solid3D`) and
  that 2D classes get Z-less geometry and 3D classes get Z-carrying geometry
  (PostGIS `ST_CoordinateDimension`). Mismatches return a clear error rather
  than a corrupt row.
- **Derived geometry is just a WKT.** "Alpha-wrap these buildings" is done by an
  existing SQL/computation step that yields a WKT solid; `create_scratchpad_item`
  merely persists it under a name, class `Solid3D`, and a color. The scratchpad
  tool doesn't need to know *how* the geometry was derived — that goes in the
  `data` JSON (e.g. `source_query`, `derived_from[]`).
- **The scratchpad is a queryable variable store, not just a bag of blobs.**
  Because items live in a real table, they can be referenced from later
  computations, e.g. *"count how many of the buildings from
  `my_nice_result2` fall inside the district polygon stored as
  `district_42`"* — the agent composes that as a spatial join (see
  [Spatial joins](#spatial-joins-with-the-3dcitydb)).

## Ownership model & scoping

"Keep intermediate results of different users from mixing, while still
recognizing a returning user" is really **two different concepts** that both
get called "session." Splitting them resolves most of the design:

| Concept | Lifetime | Where it lives | Purpose |
|---------|----------|----------------|---------|
| **Chat/conversation context** | Ephemeral — per browser tab, resets on reopen | in-memory `_sessions` dict (today's design in [runtime_tools.py](../../src/citydb_mcp/tools/runtime_tools.py): `uuid4` key, TTL GC, LRU trim) | The LLM's running conversation |
| **User identity** | Durable — survives browser close/reopen | `localStorage['citydb_user_id']` (a `uuid4` minted once) | Recognize the user; own the scratchpad rows |

**The scratchpad is owned by the durable `user_id`, not the ephemeral
conversation.** Consequences, matching the stated requirements exactly:

- **Isolation between users.** Each user has a distinct `user_id`; their items
  are invisible to every other user, *enforced by the database* (next section),
  so even a user who writes raw SQL can only see their own rows. A "completely
  fresh session" (new device, no prior `localStorage`) mints a **new
  `user_id`** → starts with an **empty scratchpad**.
- **Returning user.** Reopen the chatbot later on the same browser → the same
  `user_id` is restored from `localStorage` → the chat context starts **empty**
  (fresh conversation), but the **previous scratchpad rows come back** and are
  re-rendered in the viewer.

### Identity without login: auto-detect new vs. continuing session

The WebUI **has no login and should not get one.** The requirement is simply:
*automatically detect whether this is a new session or a user continuing an
earlier one.* That is exactly what a `localStorage`-held id gives us, with no
server-side account machinery:

- **New session** — first visit on a browser (no `citydb_user_id` in
  `localStorage`) → the client mints a fresh `uuid4`, stores it, and sends it
  with requests → the user gets an **empty scratchpad**.
- **Continuing session** — `localStorage` already holds a `citydb_user_id` →
  the client reuses it → the user's **previous scratchpad rows are restored**.

This is *automatic* from the server's perspective: it never asks "are you new
or returning," it just receives whichever id the client holds. A "new session"
is not a server-side event at all — it's a client-side consequence of an empty
`localStorage`.

The WebUI is same-origin and **already persists state in `localStorage`**
([app.py](../webui/app.py) — e.g. layout widths via
`localStorage.getItem/setItem`), so this needs no new transport. A `uuid4` in
`localStorage`:

- survives browser close/reopen and is shared across tabs in the same browser
  (which is what "recognize the same user on this machine" means here);
- is **never sent on the wire automatically** (no CSRF/HTTP plumbing, no
  privacy side-effects that cookies bring);
- needs zero server-side cookie/session/auth machinery.

**Known limitation (accepted):** `localStorage` identity is *per browser
profile*, not per human. Two people at one workstation share one id; the same
person on two different browsers gets two ids (two scratchpads). This is the
price of no-login and is acceptable for this tool. Cookies/SSO would only be
introduced if cross-device identity later becomes a real requirement.

## Isolation & security model

**The requirement:** the scratchpad must be *enforcedly* isolated per user.
A plain `user_id` column with `WHERE user_id = …` in the tool SQL is **not**
isolation — the same users can run arbitrary `SELECT` SQL against the database
(`run_query`-style tooling), and a user could simply drop the filter and print
every other user's rows. The filter must therefore live **in the database
kernel**, below any SQL the user can write.

### Decision: Row-Level Security on one shared table

```sql
CREATE TABLE citydb.scratchpad (…);          -- as above

ALTER TABLE citydb.scratchpad ENABLE ROW LEVEL SECURITY;
-- Force RLS even for the table owner / superuser-acting roles, so no one
-- bypasses it by connecting as a privileged role.
ALTER TABLE citydb.scratchpad FORCE ROW LEVEL SECURITY;

CREATE POLICY scratchpad_isolation ON citydb.scratchpad
    USING      (user_id = current_setting('app.current_user_id'))  -- read
    WITH CHECK (user_id = current_setting('app.current_user_id')); -- write
```

- Every scratchpad tool transaction begins with
  `SET LOCAL app.current_user_id = '<uid>'` (scoped to the transaction, from
  the server-side session — never from tool arguments). From that moment,
  **every** statement that touches the table — including a raw user-crafted
  `SELECT * FROM citydb.scratchpad` — silently returns **only that user's
  rows**, and `INSERT`/`UPDATE`/`DELETE` can only affect own rows. `SELECT *`
  from another user's perspective returns zero rows, not an error: there is
  nothing to "leak" or even to confirm exists.
- **No DDL at runtime.** The table, indexes, and policy are created once by an
  admin (or by a bootstrap step of the MCP server at install). Per-user setup
  is just the `SET LOCAL` — zero schema churn, zero cleanup on departed users
  (except optionally expiring stale rows).
- **One table, two GiST indexes** — no N-table index fan-out, no per-user
  `ANALYZE`, uniform maintenance.

### Alternative (your proposal): one schema per user

`CREATE SCHEMA scratchpad_<uid>;` containing a copy of the scratchpad table,
with each user's role granted `USAGE` **only on its own schema**:

- Isolation is likewise kernel-enforced (no grant → the table is unreachable,
  even via raw SQL), and **cross-join with CityGML works identically** (see
  below).
- Costs: **runtime DDL** (`CREATE SCHEMA`/`CREATE TABLE` per new user,
  `DROP` for cleanup), N tables × 2 indexes to maintain, and `pg_catalog`
  leaks the **existence** of other users' schemas (names visible via
  `information_schema.schemata`) — not their data, but not zero leakage.
- It also makes the "DDL write access only required for that schema" scoping
  real: the user role would need schema-level `CREATE` **only** within
  `scratchpad_<uid>` (or a bootstrap admin performs the DDL once, keeping user
  roles DML-only — which is what I'd do even in this variant).

**Why RLS over the alternative.** It is strictly less machinery: no runtime
DDL at all, no N-table fan-out, no existence leakage, and it keeps the
"no general DML/DDL rights" requirement intact *more* literally than the
per-user-schema variant (which needs DDL somewhere, even if confined). The
schema variant remains a viable fallback if hard schema separation is ever
wanted for operational reasons (e.g. per-user `pg_dump` / retention).

### Roles & grants

```sql
-- scratchpad role: DML on the scratchpad table only. No schema CREATE (no
-- DDL), no access to any CityGML table. Works even when users have no DML/DDL
-- at all elsewhere.
CREATE ROLE citydb_scratchpad LOGIN PASSWORD '...';
GRANT USAGE ON SCHEMA citydb TO citydb_scratchpad;
GRANT SELECT, INSERT, UPDATE, DELETE ON citydb.scratchpad TO citydb_scratchpad;
```

- The **role grant + RLS policy is the injection-proof boundary**; the `.env`
  flag is just what the LLM "sees." A prompt injection can't reach CityGML
  tables (no grant on them for this role) and can't DDL (no schema `CREATE`).
- A **second connection pool** (non read-only) connects as
  `citydb_scratchpad` and is used *only* by the scratchpad tools; `run_query`
  keeps its hard read-only boundary regardless (mirrors the two-pool design in
  [idea-for-future-write-access.md](idea-for-future-write-access.md), scoped to
  one table + one policy).
- Because blast radius is a single throwaway table, this is safe to enable in
  a multi-user setup without opening up broader writes.
- **Audit (from day one):** append a JSONL line per scratchpad
  create/update/delete (timestamp, `user_id`, item `name`, op, affected
  geometry type) to `CITYDB_MCP_AUDIT_LOG`, reusing the mechanism specced in
  the write-access doc. This is non-optional because the scratchpad tools are
  the only write path that ships by default here — it's what makes
  "the model changed/removed X" recoverable after the fact.

### `.env`

```dotenv
# Spatial scratchpad — OFF by default. Writes affect only citydb.scratchpad,
# as the scratchpad-only role + RLS (no DML/DDL needed anywhere else).
CITYDB_ALLOW_SCRATCHPAD=false
CITYDB_SCRATCHPAD_USER=
CITYDB_SCRATCHPAD_PASSWORD=

# Days of inactivity before a user's scratchpad is purged. '0' disables.
CITYDB_SCRATCHPAD_EXPIRY_DAYS=14
```

- Env flows into the stdio MCP subprocess for free
  (`env=os.environ.copy()` in
  [mcp_client.py](../webui/mcp_client.py)).

### Auto-expiry & purging stale scratchpads

Scratchpad rows are by definition *intermediate*; they should not live
forever. There is **no reliable "browser session was destroyed" event** to
hook into — clearing site data, incognito use, or simply closing the browser
all leave `localStorage` (and thus the `user_id`) intact, and a genuinely lost
`user_id` leaves *orphaned* rows (unreachable, not deletable by id). So we use
**time-based expiry**, which also covers the orphan case automatically.

**"Last seen" without a second table.** The scratchpad table already carries a
per-row `updated_at`; a user's "last seen" is `MAX(updated_at)` over their
rows. We give `updated_at` a second job — it is bumped when the user is
*active* (see placement note below) via one cheap, idempotent statement, fired
by the WebUI on the scratchpad pool under the user's GUC so RLS keeps it
scoped to their own rows:

```sql
-- fire-and-forget, once per active user turn:
UPDATE citydb.scratchpad SET updated_at = now()
WHERE user_id = current_setting('app.current_user_id');
```

**Placement: the WebUI's chat turn, not the scratchpad tools.** The bump fires
on *every* query the user sends (SQL questions included), because "the user is
active" is the real signal — a user who only asks SQL questions for weeks is
still active and must not be purged, even if they never touched a scratch
item. The MCP server's scratchpad tools do not fire it themselves; the WebUI
does, on the scratchpad pool, as part of handling the turn. (A scratchpad-only
bump would wrongly purge users who work purely through SQL.)

**Purging across users trips an RLS subtlety.** A `DELETE … WHERE updated_at <
:cutoff` must reach *all* users' stale rows, but the scratchpad role is bound
to one user per connection via RLS — it can only ever delete its *own* rows.
And the standard escape — `SET LOCAL app.current_user_id` — is unsafe here:
RLS policies can reference only GUCs (not function parameters), and a role may
reset GUCs it owns on a following statement, so a purge function that just
`SET LOCAL`s could be pointed at arbitrary users by a buggy/malicious caller.

**Solution: one `SECURITY DEFINER` function, owned by the bootstrap role.**
It takes the cutoff as an ordinary **parameter** (no GUC, no RLS involvement),
runs with the owner's privileges, and sees all users' rows because the owner
is the table owner (bypasses RLS by default):

```sql
-- Created once at bootstrap, owned by the role that created the table
-- (e.g. the import/admin role). Because SECURITY DEFINER runs with the
-- owner's privileges, Postgres grants EXECUTE to PUBLIC by default — which
-- would let ANY user role purge every scratchpad. Revoke the default and
-- grant only to the WebUI's role (NOT to citydb_scratchpad).
CREATE FUNCTION citydb.scratchpad_purge_before(p_before timestamptz)
RETURNS integer
LANGUAGE sql
SECURITY DEFINER
SET search_path = citydb
AS $$
    DELETE FROM citydb.scratchpad
    WHERE updated_at < p_before
    RETURNING 1;
$$;
REVOKE EXECUTE ON FUNCTION citydb.scratchpad_purge_before(timestamptz) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION citydb.scratchpad_purge_before(timestamptz) TO <webui_role>;
```

- Called by the WebUI's self-scheduling timer with
  `p_before = now() - interval :CITYDB_SCRATCHPAD_EXPIRY_DAYS`.
- **The `REVOKE … FROM PUBLIC` is what makes it safe.** Without it, every
  role (including `citydb_scratchpad`) could call
  `SELECT citydb.scratchpad_purge_before('1999-01-01')` and wipe all
  scratchpads. The `GRANT` is restricted to the WebUI's own role — the only
  one that runs the timer — and is never given to `citydb_scratchpad`.
- This is the **only DDL** in the feature beyond the table/index/policy, and
  it runs once at bootstrap — at runtime there is still zero DDL.

Configured by `CITYDB_SCRATCHPAD_EXPIRY_DAYS` (default **14**, `0` disables —
see the [`.env` block](#env)).

**Cadence: a self-scheduling timer in the WebUI, once per day + on (re)start.**
Because expiry is measured in *days*, running the purge more often than daily
buys nothing. A `threading.Timer` in the WebUI (see
[app.py](../webui/app.py)) runs the purge once on startup and re-schedules
itself every 24 h:

```python
# production/webui/app.py — started once, near the existing app-startup code
def _scratchpad_purge_loop():
    try:
        # Runs as the WebUI's own DB role; the function is SECURITY DEFINER
        # so it executes with the bootstrap role's privileges (sees all users).
        with scratchpad_pool.getconn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT citydb.scratchpad_purge_before(now() - "
                "make_interval(days => %s))",
                (os.environ.get("CITYDB_SCRATCHPAD_EXPIRY_DAYS", "14"),),
            )
            cur.close(); conn.commit()
    except Exception:
        logging.exception("scratchpad purge failed")
    threading.Timer(86_400, _scratchpad_purge_loop).start()

if os.environ.get("CITYDB_ALLOW_SCRATCHPAD", "false").lower() == "true":
    threading.Thread(target=_scratchpad_purge_loop, daemon=True).start()
```

No new container, no new dependency — the WebUI already has the DB
credentials and already `depends_on postgres healthy`, so the timer has
everything it needs. If the container is down for > 14 days, the next startup
purges (acceptable: a stopped service has no active users to protect). The
expiry window (14 days) is far larger than the purge interval (1 day +
startup), so a live user is never at risk: a row is deleted only after being
inactive for the *entire* window.

**Access control on the purge function.** Because
`scratchpad_purge_before` is `SECURITY DEFINER`, its power comes from the
owner's privileges — and Postgres grants `EXECUTE` to `PUBLIC` **by default**.
Left alone, *any* user role could call
`SELECT citydb.scratchpad_purge_before('1999-01-01')` and wipe every
scratchpad. The bootstrap DDL therefore **revokes the default** and grants
`EXECUTE` only to the WebUI's role:

```sql
REVOKE EXECUTE ON FUNCTION citydb.scratchpad_purge_before(timestamptz) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION citydb.scratchpad_purge_before(timestamptz) TO <webui_role>;
-- NOT granted to citydb_scratchpad (the per-user RLS role) — that role must
-- never be able to purge.
```

### How `user_id` reaches the MCP server in a multi-user WebUI

The deployment is a **multi-user shared WebUI with no login**, so `user_id`
**cannot** be a process-level constant (env-at-spawn) — one WebUI process
serves many concurrent users. The client is the source of identity:

1. The browser holds `citydb_user_id` in `localStorage` (minted on first
   visit, reused thereafter — see [Identity without
   login](#identity-without-login-auto-detect-new-vs-continuing-session)).
2. The client sends it with every request (e.g. a `X-User-Id` header or a
   field in the chat POST body).
3. The WebUI, when dispatching a tool call **server-side**, attaches that
   `user_id` to the tool context (e.g. a reserved argument name such as
   `_user_id` that the backend injects *after* the model's arguments are
   parsed, or a per-request field the MCP client forwards). It is **never
   derived from the model's argument JSON**.
4. The MCP server looks up/creates the session-scoped identity, and the
   scratchpad tools run `SET LOCAL app.current_user_id` from it.

Sanitization rule (defense in depth, the real boundary being RLS): the server
strips any `_user_id`/`user_id` key the model may have put in tool arguments.

**Note on a client-supplied id with no login.** Because the id travels from
the client, any user *could* send someone else's id — but RLS makes that
consequenceless: the id only selects *which rows this connection can see*, and
there is no path to read rows under an id you don't control (you'd just get
someone else's scratchpad view, which requires knowing their random `uuid4`).
No authentication is needed for the isolation to hold.

The same per-request `user_id` also drives the **last-seen bookkeeping** (see
[Auto-expiry](#auto-expiry--purging-stale-scratchpads)): the WebUI bumps
`updated_at` for that user on **every chat query**, not only on scratchpad
writes.

## Spatial joins with the 3DCityDB

Yes — scratchpad entries can be spatially joined with the CityGML model, and
it works identically under both isolation options:

```sql
-- how many of the buildings inside my alpha-wrapped solid `flood_cluster_3`
-- are residential?
SELECT oc.classname AS class, count(*)
FROM citydb.scratchpad sp
JOIN citydb.geometry_data g ON g.geometry_data_id IN (
        SELECT geometry_data_id FROM citydb.geometry_data
        WHERE ST_Within(
            ST_Force3DZ(g.geom),
            (SELECT geometry3D FROM citydb.scratchpad WHERE name = 'flood_cluster_3')
        ))
JOIN citydb.feature f ON f.id = g.feature_id
JOIN citydb.objectclass oc ON oc.id = f.objectclass_id
GROUP BY 1;
```

Requirements: the scratchpad role needs `USAGE ON SCHEMA citydb` + `SELECT` on
the CityGML tables it may join (a *read* grant — no writes to the model), and
both schemas must share the working CRS (they do — scratchpad geometries are
stored at `database_srs.srid` by the tool layer). Under the per-user-schema
option the qualified names become `scratchpad_<uid>.scratchpad`; the join
semantics are unchanged.

## Viewer integration (the 3D/2D rendering side)

The viewer is Cesium-based. The existing tileset auto-load
([auto-load-tileset.js](../webmap-overlay/auto-load-tileset.js)) and the
highlight/selection bridge
([highlight-bridge.js](../webmap-overlay/highlight-bridge.js),
[select-bridge.js](../webmap-overlay/select-bridge.js)) show the exact
primitives we'll reuse:

- **Styling** already uses `Cesium.Cesium3DTileStyle` + `colorBlendMode` for
  per-feature colors.
- **Camera** navigation already uses `cesiumViewer.camera.flyToBoundingSphere`
  (`flyToSphere`/`flyToTarget` in the highlight bridge).
- **Parent→viewer messaging** already exists (postMessage bridge, "Parent ->
  viewer messages" in the bridge file).

Scratchpad rendering differs from tileset highlighting: scratchpad items are
**not** in the tileset, so they render as **CesiumJS Entities** in their own
layer (a dedicated scratchpad entity collection, managed by the viewer bridge
alongside the 3D Tiles layer), not via a 3D Tiles style. The rendering API is
**CesiumJS** (the map's engine) — the existing highlight/selection bridges
already use its `Cesium` namespace
(`Cesium3DTileStyle`, `cesiumViewer.camera.flyToBoundingSphere`), so the
scratchpad layer reuses the same global. Every class has a native CesiumJS
rendering — see [Rendering notes per class geometry](#rendering-notes-per-class-geometry).

| Scratchpad class | Cesium entity property | Key settings |
|------------------|------------------------|--------------|
| `2Dmarker` | `point` (+ `label`) | `pixelSize` (screen px), `clampToGround: true`, pin-style icon, `text` = `COALESCE(label, name)` |
| `3Dmarker` | `point`/`billboard` (+ `label`) | `pixelSize`, at the 3D coordinate (not clamped), `text` = `COALESCE(label, name)` |
| `Point2D` | `point` — filled circle | `pixelSize` (screen px, min size so it never vanishes), `clampToGround: true` |
| `LineString2D` / `MultiLineString2D` | `polyline` | **`width` in screen px** (constant at any zoom → always visible), `clampToGround: true` (drape) |
| `Polygon2D` / `MultiPolygon2D` | `polygon` | `material` fill + `outline` (`color`, `width` in px), `clampToGround: true` (drape) |
| `Point3D` | `ellipsoid` — **true 3D sphere** | `radii: [r, r, r]` in **world units** (`r` from `data.size_m`, default e.g. 1 m) |
| `LineString3D` / `MultiLineString3D` | `polylineVolume` — **pipe** | circular cross-section: `width` = `extrudedWidth` (meters) from `data.size_m`, high `corners`, `material` |
| `Polygon3D` / `MultiPolygon3D` / `Surface3D` | `polygon` (per planar part) / triangle-mesh `Primitive` for triangulated surfaces | at native height (not clamped), `material` fill, `color`, `transparency` |
| `Solid3D` / `MultiSolid3D` | triangle-mesh `Primitive` (no entity class for arbitrary solids) | at native height, `material` fill, `color`, `transparency` |

### Rendering notes per class geometry

All of these are native CesiumJS capabilities (verified against the CesiumJS
source/API):

- **2D linestring thickness** — `PolylineGraphics.width` is a constant
  *pixel* width independent of zoom, so a draped line keeps a fixed on-screen
  thickness at any zoom level. `clampToGround: true` drapes it over the
  terrain.
- **2D point as a filled circle** — `PointGraphics` with a minimum
  `pixelSize` renders a filled, screen-space circle that is always visible.
  (`CircleGraphics`/`Cesium.CircleGeometry` would instead be a *geodetic*
  circle in meters that collapses to a dot when zoomed out — wrong tool for
  "the user must be able to see it.")
- **3D point as a sphere** — `EllipsoidGraphics` with `radii: [r, r, r]` is a
  true 3D sphere in world units (meters). The radius comes from `data.size_m`
  with a per-class default.
- **3D linestring as a pipe** — `PolylineVolumeGraphics` is a first-class
  **Entity** class (not just a `Primitive`): it extrudes a 2D cross-section
  along the polyline. A circular pipe sets `width` = `extrudedWidth` (meters,
  from `data.size_m`) with a high `corners` count; `cornerType`
  (`ROUNDED`/`BEVELED`) controls the joints, `granularity` controls the
  sampling density, `material` sets the fill (with `outline` + `outlineWidth`
  optional). Being an entity, it participates in the same show/hide/upsert
  messages as everything else.
- **Unified rendering path.** Markers, 2D geometries, 3D points and 3D pipes
  are all **Entities** — only arbitrary `Solid3D`/`MultiSolid3D` meshes (and
  possibly triangulated `Surface3D`s) need `Cesium.Primitive`s. The viewer
  bridge therefore manages one entity collection plus one primitive collection
  for solids.
- **Visibility sizing differs by dimension.** **2D** items use **screen-space**
  sizes (`pixelSize`/`width` in pixels) so they never shrink to invisibility
  when zoomed out — that is what guarantees visibility. **3D** items use
  **world-unit** sizes (sphere radii, pipe diameter in meters) so they have a
  real physical size in the model.
- **Per-item sizing lives in `data`.** A pipe's diameter and a sphere's radius
  vary *per item*, not per class, so the size is read from `data` (convention:
  `{"size_m": 2.0}`, free-form per the `data` decision) with a sensible
  per-class default. This keeps the schema clean and lets the agent "make that
  pipe thicker" by updating `data.size_m` and re-rendering.

**The viewer is updated on *every* mutation, not just on load.** Create,
update, delete, show, and hide all push a corresponding viewer-bridge message,
so the map always reflects the current DB state without the user re-navigating:

| Mutation | Viewer message | Effect |
|----------|----------------|--------|
| `create_scratchpad_item` | "upsert entity" | Add/replace the entity |
| `update_scratchpad_item` | "upsert entity" | Re-style / re-geometry / re-label in place |
| `delete_scratchpad_items` | "remove entities" | Drop the named entities |
| `show_scratchpad_items` / `hide_scratchpad_items` | "set visibility" | Toggle the named entities (also flips the DB flag) |
| `navigate_to_scratchpad_items` | "fly-to" | No entity change — camera only |

Message flow (any mutation):

1. Agent calls the tool → the DB row is changed (RLS binds the `user_id`) → the
   tool returns the affected item `name`(s) + full render spec (class /
   label / color / transparency / WKT).
2. The **server** (or the WebUI backend dispatching the tool result) posts the
   matching viewer message (upsert / remove / set-visibility / fly-to) to the
   bridge.
3. The bridge transforms the geometry to WGS84 (same
   `ST_Transform(geom, 4326)` pattern as
   [highlight.py](../../src/citydb_mcp/tools/highlight.py)) and applies the
   entity change to the scratchpad layer.
4. For `navigate_to_scratchpad_items` only, the bridge computes the **union of
   the named items' extents** into a single `BoundingSphere` and calls
   `flyToBoundingSphere` once — so all named items land in view together (same
   multi-target pattern the highlight bridge uses).

**Hydration on load.** Because the scratchpad is durable (owned by `user_id`),
the WebUI, on page load, fetches the current user's items
(`list_scratchpad_items`, including each item's `is_visible`) and replays the
visible ones into the scratchpad entity layer before the first chat turn —
this is what restores a returning user's items with an empty chat context (and
keeps their hidden items hidden).
Purging needs **no** proactive viewer step: purged users are by definition
offline, so their (already removed) entities are simply never re-hydrated.

Because the WebUI cloud backend currently advertises a **hard-coded tool list**
(see [idea-for-future-multiple-MCP-servers.md](idea-for-future-multiple-MCP-servers.md)
"Current State"), the new scratchpad tools must be added there and their
"render" side effects routed to the viewer bridge — the main non-DB work item.

### Where 2D draping gets tricky

- **Geographic vs projected CRS.** In a projected CRS, "terrain drape" means
  projecting each 2D vertex up onto the terrain (`clampToGround` handles this in
  Cesium). In a geographic CRS (e.g. EPSG:4326), z/height semantics differ — the
  same logic already reasoned about in
  [dynamic_tools.py](../../src/citydb_mcp/tools/dynamic_tools.py)
  (`srid_is_2d`, `is_cartesian`) should drive how the viewer treats z.
- **3D solids/surfaces** are never clamped, so they avoid the drape question
  entirely.
- **2D spatial queries** should operate on `geometry2D` and be Z-agnostic
  (the GiST index on `geometry2D` supports this); 3D proximity/containment
  uses `geometry3D` (its index includes Z).

## Phased plan

1. **MVP — bootstrap + create/list/delete.** Bootstrap DDL (admin-run, once):
   `CREATE TABLE scratchpad` + both GiST indexes + `scratchpad_geom_one_notnull`
   + RLS enable/force + isolation policy + the `SECURITY DEFINER`
   `scratchpad_purge_before` function; the scratchpad-only role with the grants
   above; the second (non read-only) pool; per-request `user_id` binding
   (client `X-User-Id` → server-injected) + `SET LOCAL app.current_user_id`;
   `create_scratchpad_item` / `list_scratchpad_items` / `get_scratchpad_item` /
   `delete_scratchpad_items` (WKT in, SRID pinned, class- and type-validated,
   name-validated; delete takes a name list). No viewer work yet; verify rows —
   **including that a second user's `user_id` sees none of the first user's
   rows, even via raw SQL** — via reads.
2. **Identity + last-seen.** Wire the client's `localStorage` id mint/reuse,
   the per-request `X-User-Id` transport, and the per-turn `updated_at` bump
   (see [Auto-expiry](#auto-expiry--purging-stale-scratchpads)). Hydration on
   page load (fetch + replay the user's items) so a returning user sees their
   previous scratchpad with an empty chat.
3. **Viewer rendering.** Scratchpad Cesium entity layer + parent→viewer
   messages. Render 3D solids/surfaces first (no drape complexity), then 2D
   draped polygons, then default-pin markers with labels. Every create/update/
   delete/show/hide must push its viewer message (live-update contract).
4. **Navigation + styling round-trip.** `navigate_to_scratchpad_items`
   (frames all named items in one view), `update_scratchpad_item` (live
   re-style), `show_…`/`hide_…`/`delete_…` (all name-list based), WebUI backend
   tool advertising + a lightweight confirmation for bulk deletes.
5. **Purge + polish.** The WebUI **`threading.Timer`** purge loop (on startup
   + daily; `CITYDB_SCRATCHPAD_EXPIRY_DAYS`), the JSONL audit on every
   scratchpad mutation, and possibly "promote a scratchpad item to CityGML"
   export.

## Decided (this session)

- **Isolation:** Row-Level Security on one shared `citydb.scratchpad` table
  (kernel-enforced, zero runtime DDL). Per-user schemas kept as a fallback only.
- **Identity:** no login; the client mints/reuses a `uuid4` in `localStorage`
  and sends it per request (`X-User-Id`). New vs. continuing is auto-detected.
- **Handles:** `name` (an identifier — no spaces; any keyword-matching
  identifier allowed; per-user unique; immutable) is the variable handle;
  nullable `label` is the on-map text (`COALESCE(label, name)`); no numeric id.
  Names are always passed as bound parameters / double-quoted, never inlined raw.
- **Geometry:** two columns (`geometry2D`/`geometry3D`) with a GiST index each;
  exactly one populated, per `class`.
- **Access:** dedicated `CITYDB_ALLOW_SCRATCHPAD` flag + scratchpad-only role
  (DML on one table) + read grants on CityGML for spatial joins; `run_query`
  stays read-only.
- **Expiry:** `updated_at` bumped on **every WebUI chat turn** (any query, not
  just scratchpad ops); `SECURITY DEFINER` `scratchpad_purge_before(cutoff)`
  run by a **`threading.Timer` in the WebUI** (on startup + re-scheduled
  every 24 h); `CITYDB_SCRATCHPAD_EXPIRY_DAYS=14` default.
- **`data` JSON:** fully free-form — the user or the chatbot decides what to
  store; the schema reserves **no** keys (e.g. `source_query`, `derived_from[]`,
  `units` are conventions, not requirements).
- **Marker rendering:** a **default pin symbol** + `color` + `label`
  (`COALESCE(label, name)`). No custom icon/shape is supported.
- **Rendering engine:** **CesiumJS** (the map's engine) is the scratchpad
  rendering API — every class maps to a native CesiumJS entity
  (`PointGraphics`, `PolylineGraphics`, `PolygonGraphics`, `EllipsoidGraphics`,
  `PolylineVolumeGraphics`, `LabelGraphics`), with `Cesium.Primitive` meshes
  only for `Solid3D`/`MultiSolid3D`.
- **Audit:** JSONL line per create/update/delete, **from day one**, to
  `CITYDB_MCP_AUDIT_LOG`.
- **Live viewer updates:** every create/update/delete/show/hide pushes a
  viewer-bridge message so the map always mirrors the DB state. Show/hide
  also persist an `is_visible` column (so visibility survives re-hydration).
  Purging needs no viewer step (purged users are offline; their entities are
  never re-hydrated).

## Open questions (for this session)

None. All design points are resolved.
