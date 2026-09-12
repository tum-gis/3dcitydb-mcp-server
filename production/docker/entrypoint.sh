#!/usr/bin/env sh
set -e

# Validate that at least one LLM provider key is configured.
if [ -z "$ANTHROPIC_API_KEY" ] && [ -z "$OPENAI_API_KEY" ] && [ -z "$OLLAMA_BASE_URL" ]; then
  echo "ERROR: No LLM provider configured."
  echo "Set at least one of: ANTHROPIC_API_KEY, OPENAI_API_KEY, OLLAMA_BASE_URL"
  exit 1
fi

# Validate database URL is present.
if [ -z "$DATABASE_URL" ]; then
  # Attempt to build from parts if available
  if [ -n "$CITYDB_HOST" ] && [ -n "$CITYDB_USER" ]; then
    export DATABASE_URL="postgresql://${CITYDB_USER}:${CITYDB_PASSWORD}@${CITYDB_HOST}:${CITYDB_PORT:-5432}/${CITYDB_NAME:-citydb}"
    echo "Built DATABASE_URL from CITYDB_* env vars."
  else
    echo "ERROR: DATABASE_URL is not set."
    echo "Either set DATABASE_URL directly, or set CITYDB_HOST, CITYDB_USER, CITYDB_PASSWORD."
    exit 1
  fi
fi

# Optionally also expose the MCP server over SSE/HTTP (port 8080). The
# container normally runs a single foreground process (the Gradio UI), so the
# SSE server is launched in the background before `exec` hands the foreground
# to webui.app. Enable with CITYDB_MCP_SSE=1 (host/port configurable).
if [ "${CITYDB_MCP_SSE:-0}" = "1" ]; then
  export MCP_SSE_HOST="${MCP_SSE_HOST:-0.0.0.0}"
  export MCP_SSE_PORT="${MCP_SSE_PORT:-8080}"
  echo "Starting citydb-mcp SSE server on ${MCP_SSE_HOST}:${MCP_SSE_PORT} (endpoint: http://${MCP_SSE_HOST}:${MCP_SSE_PORT}/sse)..."
  python -m citydb_mcp.server_sse --host "${MCP_SSE_HOST}" --port "${MCP_SSE_PORT}" &
fi

echo "Starting citydb-mcp Gradio UI (variant: ${CITYDB_MCP_VARIANT:-byod})..."
exec python -m webui.app
