# Option 2 — Docker BYOD (Bring Your Own Database)

Run a Gradio chat UI that connects to your **existing** 3DCityDB instance. No CityGML import capability — just point it at your database and start querying.

---

## Prerequisites

- Docker + Docker Compose
- A running 3DCityDB v5 PostgreSQL instance
- An API key for Anthropic, OpenAI, or a running Ollama instance

---

## Setup

### 1. Clone the repository

```bash
git clone <repo-url>
cd citygml-mcp-serve
```

### 2. Configure environment

Copy the example and fill in your values:

```bash
cp production/.env.example production/.env
```

Edit `production/.env`:

```env
# Your 3DCityDB connection
DATABASE_URL=postgresql://citydb:citydb@your-host:5432/citydb

# LLM provider — set one or more
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# OLLAMA_HOST=http://host.docker.internal:11434
```

### 3. Start

```bash
docker compose -f production/docker-compose.byod.yml up -d
```

### 4. Open the UI

Navigate to **http://localhost:7860**

---

## Using the UI

![Gradio chat UI](../assets/ui-preview.gif)

### Provider & model

In the left sidebar:

1. Select **Anthropic**, **OpenAI**, or **Ollama**
2. Choose a model from the dropdown
   - Anthropic and OpenAI models are pre-populated
   - Ollama models are fetched live from your `OLLAMA_HOST`; click **Refresh** if the list is empty
3. Adjust **Temperature** if needed (default 0.1)
4. Click **New conversation** to reset

### Chat

Type your question in the chat input. The agent calls MCP tools, queries your database, and replies in natural language.

Example questions:
- *How many buildings are in the dataset?*
- *What is the average building height?*
- *List the 10 tallest buildings with their heights.*
- *What geometry types are present in the model?*

### SQL inspector

Click the **SQL inspector** accordion below the chat to see the last SQL query the agent ran.

---

## Stop

```bash
docker compose -f production/docker-compose.byod.yml down
```

Data in your external database is unaffected.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✅ | PostgreSQL connection URL |
| `ANTHROPIC_API_KEY` | One of these | Anthropic API key |
| `OPENAI_API_KEY` | One of these | OpenAI API key |
| `OLLAMA_HOST` | One of these | Ollama base URL |
| `CITYDB_MCP_AUTH_TOKEN` | ❌ | Bearer token for MCP SSE clients |
