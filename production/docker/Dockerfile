FROM python:3.12-slim

WORKDIR /app

# System deps: libpq for psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# ─── MCP server (installed from local source) ───────────────────────────────
# We deliberately avoid `--no-deps` indirection: starlette/uvicorn are kept
# out by listing the exact runtime deps we need below. The stdio transport
# (used by the webui) does not import starlette, so omitting it sidesteps the
# Gradio 5 / starlette TemplateResponse incompatibility.
COPY pyproject.toml ./pyproject.toml
COPY README.md ./README.md
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps .

RUN pip install --no-cache-dir \
    "mcp~=1.0" \
    "psycopg2-binary~=2.9" \
    "python-dotenv~=1.0" \
    "sqlglot~=23.0"

# ─── Web UI dependencies ────────────────────────────────────────────────────
COPY production/webui/requirements.txt ./webui-requirements.txt
RUN pip install --no-cache-dir -r webui-requirements.txt

# ─── Web UI source (importable as `webui.*`) ────────────────────────────────
COPY production/webui/ ./webui/

# ─── Entrypoint ─────────────────────────────────────────────────────────────
COPY production/docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Run as non-root to limit blast radius. The /app/data volume is owned by the
# host user inside the bind mount; entrypoint stays root-readable.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7860/', timeout=5).status<500 else 1)" \
    || exit 1

ENTRYPOINT ["/entrypoint.sh"]
