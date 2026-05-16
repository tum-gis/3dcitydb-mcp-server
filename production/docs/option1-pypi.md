# Option 1 — PyPI MCP Server

Use `citydb-mcp` as a lightweight MCP server that wires directly into Claude Code, OpenCode, Cursor, or Claude Desktop. No UI, no LLM dependencies in the base install.

---

## Install

```bash
pip install citydb-mcp
```

Requires Python ≥ 3.10.

---

## Point it at your database

Either export a single connection URL:

```bash
export DATABASE_URL="postgresql://citydb:citydb@localhost:5432/citydb"
```

Or use individual variables (e.g. in a `.env` file):

```env
CITYDB_HOST=localhost
CITYDB_PORT=5432
CITYDB_NAME=citydb
CITYDB_USER=citydb
CITYDB_PASSWORD=citydb
CITYDB_SCHEMA=citydb
```

---

## Verify the connection

```bash
citydb-mcp doctor
```

All critical checks (Python version, DB connection, PostGIS, 3DCityDB schema) must show ✓. Warnings about SFCGAL or LLM providers are non-critical.

---

## Wire into your MCP client

### Claude Code

Run once in your project directory:

```bash
claude mcp add citydb -- citydb-mcp serve --transport=stdio
```

Or add manually to `.claude/mcp.json`:

```json
{
  "mcpServers": {
    "citydb": {
      "command": "citydb-mcp",
      "args": ["serve", "--transport=stdio"],
      "env": {
        "DATABASE_URL": "postgresql://citydb:citydb@localhost:5432/citydb"
      }
    }
  }
}
```

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "citydb": {
      "command": "citydb-mcp",
      "args": ["serve", "--transport=stdio"],
      "env": {
        "DATABASE_URL": "postgresql://citydb:citydb@localhost:5432/citydb"
      }
    }
  }
}
```

### OpenCode / Cursor

Use the same `command` + `args` pattern in your client's MCP config file.

### SSE transport (network clients)

```bash
citydb-mcp serve --transport=sse --host=0.0.0.0 --port=8765
```

Then point your client at `http://localhost:8765/sse`.

---

## Available tools

Once connected, your agent has access to:

| Tool | Description |
|---|---|
| `assemble_prompt` | Builds the full system prompt — call once at session start |
| `run_query(sql)` | Read-only SELECT; results capped at 500 rows |
| `scan_objectclasses` | Discovers object class hierarchy |
| `resolve_properties(objectclass_id)` | Properties, value columns, codelists |
| `get_generic_attributes` | Generic attributes with categorical detection |
| `get_db_context_snapshot` | SRS, bounding box, feature counts, LoD |
| `get_lod_config` | Available Levels of Detail |
| `get_examples(objectclass_ids)` | Curated SQL examples for your object classes |
| `get_database_schema` | Table structures, columns, foreign keys |
| `get_query_guidelines` | SQL best practices and indexed columns |

---

## Read-write mode (optional)

By default the server is read-only. To allow an agent to edit geometry and properties:

```bash
citydb-mcp serve --mode=readwrite --i-understand-the-risks
```

See [MODES.md](../../MODES.md) for the full risk model, access control layers, and write tool reference.

---

## CLI reference

```
citydb-mcp serve    [--mode readonly|readwrite] [--transport stdio|sse]
                    [--host HOST] [--port PORT]
                    [--database-url URL] [--auth-token TOKEN]
                    [--log-level LEVEL] [--i-understand-the-risks]

citydb-mcp doctor
citydb-mcp version  [--database-url URL]
```
