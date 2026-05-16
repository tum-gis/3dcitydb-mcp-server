# Option 3 — Docker Fullstack

Spin up a complete stack: a fresh 3DCityDB v5 instance (with SFCGAL), the MCP server, and a Gradio UI with a **CityGML Import** tab. Drop a `.gml` file into `./production/data/`, click Import, and start querying.

---

## Prerequisites

- Docker + Docker Compose (with Docker socket accessible)
- An API key for Anthropic, OpenAI, or a running Ollama instance

---

## Setup

### 1. Clone the repository

```bash
git clone <repo-url>
cd citygml-mcp-serve
```

### 2. Configure environment

```bash
cp production/.env.example production/.env
```

Edit `production/.env`:

```env
# LLM provider — set one or more
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# OLLAMA_HOST=http://host.docker.internal:11434

# Database credentials (leave defaults unless you change them)
POSTGRES_DB=citydb
POSTGRES_USER=citydb
POSTGRES_PASSWORD=citydb
```

### 3. Start the stack

```bash
docker compose -f production/docker-compose.fullstack.yml up -d
```

This starts:
- **`postgres`** — 3DCityDB v5 with SFCGAL on the internal Docker network
- **`citydb-agent`** — Gradio UI at http://localhost:7860

Wait ~30 seconds for the database to initialize on first run (the health check will gate the agent startup).

### 4. Open the UI

Navigate to **http://localhost:7860**

---

## Importing a CityGML file

![Import tab](../assets/import-preview.gif)

1. Copy your `.gml` (or `.xml`) file into `./production/data/`
2. In the Gradio UI, click the **Import CityGML** tab
3. Select your file from the dropdown and click **Import**
4. Watch the log stream — the importer runs `citydb-tool import citygml` inside a Docker container
5. When the log shows `Import finished`, switch to the **Chat** tab and start querying

### What happens under the hood

The Gradio app uses the Python Docker SDK to run:

```
docker run --rm \
  --network <internal-network> \
  -v ./production/data:/data \
  3dcitydb/citydb-tool:2.1.0 \
  import citygml /data/<your-file>
```

No shell-out — the Docker SDK call is programmatic and streams stdout/stderr back to the UI in real time.

---

## Chat

Same as Option 2 — see [option2-docker-byod.md](option2-docker-byod.md#using-the-ui) for the chat UI reference.

---

## Stop

```bash
docker compose -f production/docker-compose.fullstack.yml down
```

To also remove the database volume (deletes all imported data):

```bash
docker compose -f production/docker-compose.fullstack.yml down -v
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | One of these | Anthropic API key |
| `OPENAI_API_KEY` | One of these | OpenAI API key |
| `OLLAMA_HOST` | One of these | Ollama base URL |
| `POSTGRES_DB` | ❌ | Database name (default: `citydb`) |
| `POSTGRES_USER` | ❌ | Database user (default: `citydb`) |
| `POSTGRES_PASSWORD` | ❌ | Database password (default: `citydb`) |
| `SRID` | ❌ | Spatial reference ID (default: `25832`) |

---

## Troubleshooting

**Import fails with "container not found"**
The Gradio container needs access to the Docker socket. Verify that `docker-compose.fullstack.yml` mounts `/var/run/docker.sock`.

**"No models found" for Ollama**
Your `OLLAMA_HOST` must be reachable from inside the container. Use `http://host.docker.internal:11434` on Mac/Windows or the host's LAN IP on Linux.

**Database takes too long to start**
On first run the SFCGAL-patched image initializes the 3DCityDB schema. Check logs with:
```bash
docker compose -f production/docker-compose.fullstack.yml logs postgres
```
