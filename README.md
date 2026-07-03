# 3DCityDB MCP Server

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server giving AI assistants direct, natural language access to semantic 3D city models in CityGML managed within a [**3DCityDB v5**](https://github.com/3dcitydb/3dcitydb) geodatabase.

It dynamically resolves CityGML object classes, properties, codelists, and generic attributes from the database so the AI can answer both spatial and semantic queries stated in natural language, write and execute SQL queries, and reason about CityGML data — without any manual prompt engineering. 
By including the MCP server in agentic coding environments, it becomes easy to create software that can read and write complex structured 3D city models compliant to the [OGC CityGML standard](https://www.ogc.org/standards/citygml/) (and using [3DCityDB V5](https://github.com/3dcitydb/3dcitydb) as the data repository).

Furthermore, a **Chat Assistant** is included offering a simple GUI for interactive query asking, reasoning, and answering. It is an agentic AI tool based on [LangChain](https://github.com/langchain-ai) utilising the [ReAct pattern](https://reference.langchain.com/javascript/langchain-react) for carrying out multi-step reasoning and automated error corrections. The Chat Assistent currently can be configured to work with OpenAI and Anthropic commercial LLMs as well as with locally running [Ollama](https://github.com/ollama/ollama) LLMs. For example, when using the [`qwen3.6:27b`](https://ollama.com/library/qwen3.6:27b) LLM running in Ollama, the Chat Assistant is capable of performing very complex analyses on any kind of stored 3D city model. 

---

## Features

- **Dynamic schema resolution** — walks the CityGML class hierarchy to discover available object classes and their properties
- **Property filtering** — only includes properties that actually exist in the database
- **Codelist resolution** — fetches code meanings only for codes present in the DB
- **Generic attribute enrichment** — automatic categorical detection for generic attributes
- **Read-only query execution** — `run_query` enforces SELECT-only; writes are blocked
- **Prompt assembly** — `assemble_prompt` orchestrates all tools into a complete system prompt in one call
- **Gradio chat UI** — browser-based interface with multi-LLM support (Anthropic, OpenAI, Ollama)
- **CityGML 1.0-3.0/CityJSON import** — one-click import via the Gradio UI (fullstack Docker mode only)

---

## Deployment Options

There are three ways to run the 3DCityDB MCP Server:

| | Option 1: PyPI | Option 2: Docker BYOD | Option 3: Docker Fullstack |
|---|---|---|---|
| **Best for** | Claude Code / Claude Desktop power users | Existing 3DCityDB instances | Starting from a `.gml` file |
| **Requires** | Python 3.10+, running 3DCityDB | Docker, running 3DCityDB | Docker only |
| **Gradio UI** | No (uses your AI client directly) | Yes (`localhost:7860`) | Yes (`localhost:7860`) |
| **CityGML/CityJSON import** | Manual | Manual | Via Gradio UI |
| **Database** | Your own | Your own | Bundled (PostgreSQL + PostGIS + SFCGAL) |

---

## Option 1: PyPI Package

Install the MCP server as a Python package and connect it to Claude Code, Claude Desktop, or any MCP-compatible client.

### Prerequisites

- Python 3.10 or later
- A running **3DCityDB v5** PostgreSQL instance with PostGIS

### Installation

```bash
pip install 3dcitydb-mcp-server
```

Or install from source for development:

```bash
git clone https://github.com/tum-gis/3dcitydb-mcp-server.git
cd 3dcitydb-mcp-server
pip install -e .
```

### Configuration

Copy the example environment file and edit it:

```bash
# Linux / macOS
cp .env.example .env

# Windows (PowerShell)
Copy-Item .env.example .env
```

Then fill in your connection details:

```env
# 3DCityDB PostgreSQL connection
CITYDB_HOST=localhost
CITYDB_PORT=5432
CITYDB_NAME=citydb
CITYDB_USER=postgres
CITYDB_PASSWORD=your_password_here
CITYDB_SCHEMA=citydb

# Query behaviour (optional)
CATEGORICAL_THRESHOLD=20
SAMPLE_VALUES_COUNT=5

# LLM API keys (only needed for the LangChain agent CLI, not for Claude Code/Desktop)
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# OLLAMA_BASE_URL=http://localhost:11434
```

The server loads `.env` automatically by searching upward from the working directory.

### Verify your installation

```bash
3dcitydb-doctor
```

Checks Python version, required packages, database connectivity, PostGIS/SFCGAL extensions, and the 3DCityDB v5 schema. Exits 0 if all critical checks pass.

### Connect to Claude Code (recommended)

From the directory containing your `.env`:

```bash
claude mcp add 3dcitydb -- 3dcitydb-mcp
claude
```

The MCP server starts automatically when you open a Claude session. Use `/mcp` inside the session to confirm it is connected.

### Connect to Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "3dcitydb": {
      "command": "3dcitydb-mcp",
      "cwd": "/path/to/your/project"
    }
  }
}
```

Restart Claude Desktop. The MCP server will be listed in Settings → Developer → MCP Servers.

### SSE transport (remote / production)

Run the server over HTTP for remote clients:

```bash
3dcitydb-mcp-sse --host 0.0.0.0 --port 8080
```

- Clients connect via: `http://your-server:8080/sse`
- Health check: `http://your-server:8080/health`

### LangChain agent CLI (optional)

A standalone CLI agent that uses the MCP tools directly:

```bash
3dcitydb-agent
```

Requires `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `OLLAMA_BASE_URL` in your `.env`.

---

## Option 2: Docker — BYOD (Bring Your Own Database)

Run the Gradio chat UI as a Docker container, connected to your existing 3DCityDB instance.

### Prerequisites

- Docker with Compose (V2)
- A running **3DCityDB v5** PostgreSQL instance accessible from the Docker host

> **⚠️ Spatial function support:** The AI agent uses SFCGAL functions (`CG_Volume`, `CG_3DArea`, `CG_MakeSolid`) for geometry calculations. These require PostGIS to be compiled with SFCGAL support.
>
> If your database lacks SFCGAL, volume and 3D area queries will fail silently or return errors. To get full spatial support, use **Option 3 (Fullstack)** instead — it ships a pre-patched `3dcitydb-pg` image with PostGIS + SFCGAL already enabled.
>
> You can verify SFCGAL availability on your instance with:
> ```sql
> SELECT postgis_sfcgal_version();
> ```
> If this returns an error, spatial queries will not work.

### Quick Start

```bash
# 1. Clone the repository (or just download docker-compose.byod.yml + .env.example)
git clone https://github.com/tum-gis/3dcitydb-mcp-server.git
cd 3dcitydb-mcp-server/production

# 2. Copy and edit the environment file
cp .env.example .env   # Linux / macOS
# Copy-Item .env.example .env   # Windows PowerShell
```

Edit `.env` with your database connection and at least one LLM API key:

```env
# Your existing 3DCityDB instance
CITYDB_HOST=your-db-host
CITYDB_PORT=5432
CITYDB_NAME=citydb
CITYDB_USER=citydb
CITYDB_PASSWORD=your_password
CITYDB_SCHEMA=citydb

# At least one LLM provider (the UI auto-selects based on what is available)
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# OLLAMA_BASE_URL=http://host.docker.internal:11434
```

```bash
# 3. Pull the pre-built image and start (works on all platforms, no build needed)
docker compose -f docker-compose.byod.yml up -d

# 4. Open the UI
# http://localhost:7860
```

The pre-built image (`khaoulakanna1/citydb-mcp-agent:latest`) is pulled automatically from Docker Hub on first run.

### What it includes

- **Gradio chat UI** — natural-language interface to your 3DCityDB
- **MCP server** — spawned automatically as a subprocess inside the container
- **Auto provider detection** — the UI selects Anthropic, OpenAI, or Ollama based on which keys are present in `.env`

### Gradio UI overview

| Tab | What it does |
|-----|-------------|
| **Chat** | Send natural-language questions; the agent writes and executes SQL automatically |
| **SQL Inspector** | Shows the last SQL query dispatched to the database (below the chat input) |
| **MCP Inspector** | Lists all active MCP tools and lets you refresh the assembled system prompt |
| **System Prompt** | Displays the full assembled system prompt sent to the LLM — useful for debugging |

While the agent is working, the chat bubble shows live status: *Thinking…* → *Running query…* → *Interpreting results…*

> **Ollama users:** Models without native tool-calling support (e.g. Qwen3 with extended thinking enabled) are handled automatically via a prompt-based fallback — no configuration needed. Expect roughly two LLM round-trips per question instead of one.
>
> **Prompt mode (auto):** Models with ≥ 14 B parameters receive the full system prompt; smaller models receive a compact version to fit the context window. Override this per-query with the **Prompt mode** radio button in the UI (Auto / Compact / Full).

### Building locally (optional)

If you want to build the image from source instead of pulling it:

```bash
# Linux / macOS
docker compose -f docker-compose.byod.yml up -d --build

# Windows — Docker BuildKit has a known ordering bug on Windows/NTFS.
# Disable it for local builds:
$env:DOCKER_BUILDKIT=0; docker compose -f docker-compose.byod.yml up -d --build
```

> **Windows note:** The `DOCKER_BUILDKIT=0` flag is only needed when building locally.
> Pulling the pre-built image (`docker compose up -d` without `--build`) works on Windows without any workaround.

### Useful commands

```bash
# View logs
docker compose -f docker-compose.byod.yml logs -f

# Stop
docker compose -f docker-compose.byod.yml down
```

---

## Option 3: Docker — Fullstack (Bundled PostgreSQL)

Run everything — PostgreSQL (with PostGIS and SFCGAL), the 3DCityDB schema, the MCP server, and the Gradio UI — in a single Docker Compose stack. No pre-existing database needed.

### Prerequisites

- Docker with Compose (V2)
- A CityGML or CityJSON file to import (optional — the database starts empty)

### Quick Start

```bash
# 1. Clone the repository (or just download docker-compose.fullstack.yml + .env.example)
git clone https://github.com/tum-gis/3dcitydb-mcp-server.git
cd 3dcitydb-mcp-server/production

# 2. Copy and edit the environment file
cp .env.example .env   # Linux / macOS
# Copy-Item .env.example .env   # Windows PowerShell
```

Edit `.env`:

```env
# PostgreSQL settings for the bundled database
POSTGRES_DB=citydb
POSTGRES_USER=citydb
POSTGRES_PASSWORD=citydb
SRID=25832          # EPSG code for your data's coordinate system

# At least one LLM provider
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# OLLAMA_BASE_URL=http://host.docker.internal:11434
```

```bash
# 3. (Optional) Place your CityGML file in the data directory
mkdir -p data
cp /path/to/your/city.gml data/

# 4. Pull the pre-built image and start (works on all platforms, no build needed)
docker compose -f docker-compose.fullstack.yml up -d

# 5. Open the UI
# http://localhost:7860
```

Both images are pulled automatically from Docker Hub on first run. The first start takes ~60 seconds while PostgreSQL initialises.

### Building locally (optional)

```bash
# Linux / macOS
docker compose -f docker-compose.fullstack.yml up -d --build

# Windows — disable BuildKit to avoid a known NTFS ordering bug:
$env:DOCKER_BUILDKIT=0; docker compose -f docker-compose.fullstack.yml up -d --build
```

> **Windows note:** Only needed when building locally with `--build`.
> The default `docker compose up -d` (pull from Docker Hub) works on Windows without any workaround.

### Import CityGML/CityJSON

Once the UI is open:

1. Go to the **Import CityGML/CityJSON** tab
2. Click **Refresh** to see files in `./production/data/`
3. Select your file and click **Import**
4. Watch the live log — the import runs using the Docker container [`ghcr.io/3dcitydb/citydb-tool`](https://github.com/3dcitydb/citydb-tool). Note, this container is pulled automatically, if it is not available in your Docker environment so far. In this case, please be patient as it might take 30 seconds before the import process really starts.

> The data directory is mounted at `./production/data/` on the host and `/app/data/` inside the container.

### Coordinate reference system

Set `SRID` to the EPSG code for your data before the first start. Common values:

| Region | CRS | SRID |
|--------|-----|------|
| Germany (UTM Zone 32N) | ETRS89 / UTM Zone 32N | `25832` |
| Germany (UTM Zone 33N) | ETRS89 / UTM Zone 33N | `25833` |
| USA (NAD83 / UTM Zone 14N) | NAD83 | `26914` |
| Global (WGS84) | WGS 84 | `4326` |

### Useful commands

```bash
# View logs
docker compose -f docker-compose.fullstack.yml logs -f

# Stop (preserves database volume)
docker compose -f docker-compose.fullstack.yml down

# Stop and delete all data
docker compose -f docker-compose.fullstack.yml down -v
```

---

## Configuration Reference

All options are set via environment variables (`.env` file or Docker Compose `environment` block).

### Database connection

| Variable | Default | Description |
|----------|---------|-------------|
| `CITYDB_HOST` | `localhost` | PostgreSQL host |
| `CITYDB_PORT` | `5432` | PostgreSQL port |
| `CITYDB_NAME` | `citydb` | Database name |
| `CITYDB_USER` | `citydb` | Database user |
| `CITYDB_PASSWORD` | *(required)* | Database password |
| `CITYDB_SCHEMA` | `citydb` | 3DCityDB schema name |
| `DATABASE_URL` | *(auto-built)* | Full PostgreSQL URL (overrides individual vars) |

### Fullstack only

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_DB` | `citydb` | Database name for bundled PostgreSQL |
| `POSTGRES_USER` | `citydb` | Database user for bundled PostgreSQL |
| `POSTGRES_PASSWORD` | `citydb` | Database password for bundled PostgreSQL |
| `SRID` | `25832` | EPSG code for the 3DCityDB spatial reference |
| `POSTGIS_SFCGAL` | `true` | Enable SFCGAL extension (required for `CG_Volume`, `CG_3DArea`) |

### LLM providers

At least one must be configured for the Docker variants. The Gradio UI auto-selects the provider based on what is available (Anthropic → OpenAI → Ollama, in that priority order).

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key (`sk-ant-...`) |
| `OPENAI_API_KEY` | OpenAI API key (`sk-...`) |
| `OLLAMA_BASE_URL` | Ollama base URL (e.g. `http://host.docker.internal:11434`) |

### Query behaviour

| Variable | Default | Description |
|----------|---------|-------------|
| `CATEGORICAL_THRESHOLD` | `20` | Max distinct values before a column is treated as categorical |
| `SAMPLE_VALUES_COUNT` | `5` | Number of sample values shown per non-categorical column |

### Ollama tuning (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_NUM_CTX` | `32768` | Context window size (tokens) passed to the Ollama model |
| `LOCAL_MAX_TOKENS` | `16000` | Maximum tokens the local model may generate per response |
| `OLLAMA_TIMEOUT` | `300` | Timeout in seconds for Ollama requests |

---

## Available MCP Tools

### Static (cached per session)

| Tool | Description |
|------|-------------|
| `get_database_schema` | 3DCityDB v5 table structures and foreign key relationships |
| `get_query_guidelines` | SQL best practices and optimisation tips for 3DCityDB |

### Dynamic (called at session start)

| Tool | Description |
|------|-------------|
| `scan_objectclasses` | Discover available object classes with full CityGML hierarchy |
| `resolve_properties(objectclass_id)` | Resolve properties with codelists for a given class |
| `get_generic_attributes` | Generic attributes with categorical detection |
| `get_db_context_snapshot` | SRS, bounding box, feature counts, database statistics |
| `get_lod_config` | Available Levels of Detail in the database |
| `get_examples(objectclass_ids)` | SQL examples filtered to existing object classes |

### Runtime (per query)

| Tool | Description |
|------|-------------|
| `run_query(sql)` | Execute read-only SQL (SELECT/WITH only) against 3DCityDB |
| `get_session_context` | Session management and state |
| `update_module_selection` | Narrow scope to specific object classes |
| `get_history` | Conversation history for a session |
| `submit_feedback` | Log query feedback |

### Assembly

| Tool | Description |
|------|-------------|
| `assemble_prompt` | Orchestrates all tools into a complete system prompt in one call |

---

## Architecture

```
  Claude Code / Claude Desktop / any MCP client
                      │
               MCP Protocol (stdio / SSE)
                      │
       ┌──────────────┴──────────────┐
       │   3DCityDB MCP Server       │
       │   assemble_prompt()         │
       │   scan_objectclasses()      │
       │   run_query()               │
       └──────────────┬──────────────┘
                      │
                 3DCityDB v5
               (PostgreSQL + PostGIS)


  Browser ──► Gradio UI (port 7860)           [Docker variants only]
                      │
          ┌───────────┴────────────┐
          │                        │
   Anthropic / OpenAI          Ollama (local)
   LiteLLM cloud backend       LangChain ReAct
                                (ChatOllama)
          │                        │
          └───────────┬────────────┘
                      │
               MCP Client (spawns citydb-mcp subprocess)
                      │
               3DCityDB MCP Server
                      │
                 3DCityDB v5
```

---

## Citation

This work was developed at the [Chair of Geoinformatics](https://www.asg.ed.tum.de/gis/startseite/), TUM, in the group of Prof. Dr. Thomas H. Kolbe.

---

## License

The 3DCityDB MCP server is distributed under the Apache License 2.0. See [LICENCE](LICENCE) for details.
