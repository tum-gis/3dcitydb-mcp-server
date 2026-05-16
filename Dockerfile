FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

EXPOSE 8080

CMD ["3dcitydb-mcp-sse", "--host", "0.0.0.0", "--port", "8080"]
