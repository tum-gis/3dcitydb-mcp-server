# Idea: OpenStreetMap Interface for the ChatBot (Nominatim / Overpass)

## Goal

Give the ChatBot access to **OpenStreetMap data** via the public Nominatim and
Overpass APIs, so that:

1. The model can **geocode** place names and addresses ("Where is the
   Marienplatz in Munich?") and **reverse-geocode** coordinates to addresses.
2. It can run **spatial queries against OSM** ("Which hospitals, schools,
   parks are within 1 km of the selected building?").
3. OSM results can be **combined with the 3DCityDB data**, e.g.
   "Which buildings in our dataset are near a U-Bahn station?" — enriching
   CityGML queries with real-world context that is not in the database.

This is a natural complement to the web-research idea
([idea-for-future-websearch.md](idea-for-future-websearch.md)): where that document covers general web
search and document inspection, this one covers **geodata queries** against a
specific, well-structured open dataset.

## Why an MCP Server (not custom code)

Unlike the web-research use case, a mature, off-the-shelf MCP server already
exists for exactly this. Writing a custom one would add nothing.

## Available MCP Servers

### 1. `@cyanheads/openstreetmap-mcp-server` (recommended)

- TypeScript, Apache-2.0, actively maintained (regular releases).
- Runs via **stdio** or **Streamable HTTP**; `npx`/`bunx` or Docker image.
- **No API key required** — Nominatim and Overpass are public APIs.

Six tools:

| Tool | Function |
|------|----------|
| `openstreetmap_search_places` | Geocoding via Nominatim (free-form query or structured address fields; country/layer filtering; importance-ordered results) |
| `openstreetmap_reverse_geocode` | lat/lon → nearest address, with zoom-level control (18=building … 3=country) |
| `openstreetmap_lookup_objects` | Full Nominatim address records for known OSM object IDs (N/W/R-prefixed, up to 50) |
| `openstreetmap_query_nearby` | Overpass: OSM features within a radius of a point (amenity shortcuts or arbitrary tag key/value; up to 50 km, 500 results) |
| `openstreetmap_query_bbox` | Overpass: OSM features within a bounding box |
| `openstreetmap_query_raw` | Arbitrary Overpass QL for advanced queries (multi-type, union, relation membership, historical) |

Properties relevant to this project:

- **Self-hosted friendly** — `OSM_NOMINATIM_BASE_URL` /
  `OSM_OVERPASS_BASE_URL` point at private/mirror instances (same logic as
  the self-hosted SearXNG in the web-research idea).
- **OSM policy compliant** — configurable `User-Agent` (required by the
  Nominatim usage policy), client-side Overpass concurrency cap
  (`OSM_OVERPASS_MAX_CONCURRENCY`), endpoint failover via
  `OSM_OVERPASS_ENDPOINTS` (a throttled endpoint is advanced past within the
  same call).
- **Structured error contracts** with actionable recovery hints
  (`rate_limited`, `query_timeout`, `result_too_large`, …) — good fit for
  the agent loop's error-retry behavior.
- **ODbL attribution** on every response — the model can surface the
  license notice as the ODbL license requires.
- **Cross-tool chaining** — Overpass results carry `osm_type` + `osm_id`
  that feed directly into `openstreetmap_lookup_objects`.

Client configuration (for reference; in this project it would live in `.env`
via the multi-server registry, see
[idea-for-future-multiple-MCP-servers.md](idea-for-future-multiple-MCP-servers.md)):

```json
{
  "mcpServers": {
    "openstreetmap": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@cyanheads/openstreetmap-mcp-server@latest"],
      "env": { "MCP_TRANSPORT_TYPE": "stdio" }
    }
  }
}
```

### 2. `mgcmshr/osm-places-mcp`

Small JavaScript server, 3 tools (`geocode`, `find_places`,
`find_vegetarian_restaurants`), ~0 stars, last updated months ago. Superseded
by the first option; no reason to use it.

### 3. Official example: `map-server` (`modelcontextprotocol/ext-apps`)

Reference example with a Nominatim `geocode` tool and a CesiumJS globe
(`show-map`). UI-oriented example, not a production data server.

## Integration into This Project

### `.env` (per the multi-server convention)

```dotenv
MCP_SERVER_OSM_ENABLED=true
MCP_SERVER_OSM_COMMAND=npx
MCP_SERVER_OSM_ARGS=-y @cyanheads/openstreetmap-mcp-server@latest
OSM_USER_AGENT=3dcitydb-chatbot/0.x (+self-hosted)

# optional — for heavy/regular use, point at self-hosted instances:
# OSM_NOMINATIM_BASE_URL=http://nominatim:8080
# OSM_OVERPASS_BASE_URL=http://overpass:8080
# OSM_OVERPASS_ENDPOINTS=https://overpass-api.de/api/interpreter,https://overpass.private.coffee/api/interpreter
```

Note the container needs Node.js ≥ 24 or Bun for the `npx` invocation, or use
the server's published **Docker image** as a compose service (cleaner: no
Node runtime added to the agent image).

### System prompt

Append a short "OpenStreetMap" section: when to use OSM tools vs. `run_query`
(OSM = real-world context not in the CityGML dataset; DB = the curated
building data), and that OSM results are ODbL-licensed community data of
variable quality — attribute them as such.

### Coordinate-System Caveat (important)

The 3DCityDB dataset is in **EPSG:25832** (ETRS89 / UTM 32N, Munich area);
Nominatim and Overpass return **WGS84 (EPSG:4326) lat/lon**. Any
cross-source operation needs an explicit transform:

- *DB → OSM* (e.g. "what OSM amenities are near this building?"): get the
  building's WGS84 coordinates first —
  `ST_X(ST_Transform(ST_Centroid(envelope), 4326))` etc. — then pass them to
  the OSM tools.
- *OSM → DB* (e.g. "which of our buildings are near U-Bahn station X?"):
  geocode the station to lat/lon, then in SQL:
  `ST_DWithin(geom, ST_Transform(ST_SetSRID(ST_MakePoint(lon, lat), 4326), 25832), radius_m)`.

This pattern should be documented in the system-prompt section so the model
performs the transform correctly instead of comparing 25832 meters with
degrees.

### Endpoint strategy

The public Overpass endpoint (overpass-api.de) is heavily throttled. Options:

1. **Start simple** — public endpoints + the server's built-in failover
   (`OSM_OVERPASS_ENDPOINTS`) and rate-limit-aware behavior.
2. **Regular use** — self-host an Overpass instance (official Docker images
   exist) and pin it via `OSM_OVERPASS_BASE_URL`. Same self-hosted logic as
   SearXNG; a global OSM PBF load is multi-GB, so a regional/DE extract is a
   reasonable middle ground.
3. Nominatim self-hosting is heavier (Postgres + large DB) and usually only
   worth it for high-volume deployments; the public instance with a proper
   `User-Agent` is fine at chat-bot query rates.

### Result-size discipline

Same concern as the web-research idea: `openstreetmap_query_raw` can return
500 elements with full tag sets. Prefer the convenience tools
(`query_nearby`/`query_bbox` with `limit`) and keep radii small (the server
itself advises < 5 km for dense urban POI queries).

## Effort

| Piece | Effort |
|-------|--------|
| `.env` entries + Node runtime or Docker compose service | ~1 hour |
| System-prompt section incl. EPSG:4326 ↔ 25832 transform patterns | a few hours |
| (Prerequisite) multi-server registry plumbing | shared with [idea-for-future-multiple-MCP-servers.md](idea-for-future-multiple-MCP-servers.md) |
| Optional: self-hosted Overpass container | half a day |

Because the MCP server already exists and is well-engineered, this idea is
**cheaper than the web-research one** — essentially configuration plus prompt
work on top of the shared multi-server foundation.
