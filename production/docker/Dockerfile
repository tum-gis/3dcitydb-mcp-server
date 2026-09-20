# ─── 3DCityDB Web Map Client (3D viewer; only used when ENABLE_VIZ=true) ────
# Reuse the official image as a source of the built client instead of cloning
# and building it here: this image has no git/node. That image clones the
# upstream repo to /var/www, so the client page is /var/www/3dwebclient/index.html
# and its dependencies sit beside it — copy the whole tree.
ARG WEBMAP_IMAGE=tumgis/3dcitydb-web-map:alpine-v2.0.0
FROM ${WEBMAP_IMAGE} AS webmap

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

# ─── 3D viewer: web map client + our highlight scripts ──────────────────────
# Served at /webmap by webui.app (WEBMAP_DIR). The overlay adds the postMessage
# bridge to the chat UI and auto-loads /tiles/tileset.json; its <script> tags go
# last in index.html because they use globals defined by the client's script.js.
COPY --from=webmap /var/www/ /app/webmap/
# Not needed to serve the client (about 55 MB): sample data, demos, the image's own node server deps.
RUN rm -rf /app/webmap/examples /app/webmap/demos /app/webmap/node_modules /app/webmap/data
COPY production/webmap-overlay/ /app/webmap/3dwebclient/
RUN set -e; f=/app/webmap/3dwebclient/index.html; \
    grep -q 'script.js' "$f"; \
    sed -i 's#</body>#<script src="highlight-bridge.js"></script><script src="auto-load-tileset.js"></script><script src="auto-load-terrain.js"></script></body>#' "$f"; \
    grep -q 'highlight-bridge.js' "$f"
ENV WEBMAP_DIR=/app/webmap

# ─── Entrypoint ─────────────────────────────────────────────────────────────
COPY production/docker/entrypoint.sh /entrypoint.sh
# Normalize line endings: on Windows checkouts (core.autocrlf=true) the script
# is CRLF, which would break the shebang (#!/bin/sh\r) at container start.
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

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
