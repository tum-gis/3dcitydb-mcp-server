FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

EXPOSE 8811

# Binding to 0.0.0.0 makes the server require a bearer token: set
# CITYDB_MCP_AUTH_TOKEN, or read the generated one from the file named in the
# log (CITYDB_MCP_AUTH_TOKEN_FILE overrides its location).
CMD ["3dcitydb-mcp-sse", "--host", "0.0.0.0", "--port", "8811"]
