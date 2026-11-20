"""Authentication and transport-security tests for the HTTP/SSE server.

No database is needed: ``db`` is patched where the health check touches it.

Usage:
    pytest tests/test_server_sse_auth.py -v
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

pytest.importorskip("mcp.server.transport_security")
from starlette.testclient import TestClient  # noqa: E402

from citydb_mcp import server_sse, tool_context  # noqa: E402

TOKEN = "s3cret-test-token"
BASE = "http://localhost:8811"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "pytest", "version": "0"}},
}


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    for var in ("CITYDB_MCP_AUTH_TOKEN", "CITYDB_MCP_ALLOWED_HOSTS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CITYDB_MCP_AUTH_TOKEN_FILE", str(tmp_path / "mcp_auth_token"))
    # build_app() switches _ctx trust off; restore it for the other tests.
    monkeypatch.setattr(tool_context, "trust_client_ctx", tool_context.trust_client_ctx)


def _bearer(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client():
    app = server_sse.build_app(token=TOKEN)
    with TestClient(app, base_url=BASE) as c:
        yield c


# ── 401 without / with a wrong token ─────────────────────────────────────────

@pytest.mark.parametrize("headers", [{}, _bearer("wrong"), {"Authorization": TOKEN},
                                     {"Authorization": f"Basic {TOKEN}"}])
@pytest.mark.parametrize("method,path", [("GET", "/sse"),
                                         ("POST", "/messages/?session_id=0"),
                                         ("POST", "/mcp/"),
                                         ("POST", "/mcp")])
def test_rejects_missing_or_wrong_token(client, method, path, headers):
    resp = client.request(method, path, headers={**MCP_HEADERS, **headers},
                          json=INIT if method == "POST" else None)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Bearer")


# ── Correct token passes the auth layer ──────────────────────────────────────

def test_correct_token_reaches_streamable_http(client):
    resp = client.post("/mcp/", headers={**MCP_HEADERS, **_bearer()}, json=INIT)
    assert resp.status_code == 200
    assert resp.json()["result"]["serverInfo"]["name"]


def test_correct_token_reaches_sse_messages(client):
    # Unknown session: the SDK answers (400/404), i.e. auth did not block it.
    resp = client.post("/messages/?session_id=00000000000000000000000000000000",
                       headers={**MCP_HEADERS, **_bearer()}, json=INIT)
    assert resp.status_code not in (401, 403, 421)
    assert resp.status_code in (400, 404)


def _call_asgi(app, path, headers=()):
    """Run one HTTP request through an ASGI app; return (status, headers)."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": path, "query_string": b"",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers]}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], dict(start["headers"])


def test_middleware_passes_sse_with_correct_token():
    # /sse streams forever, so check the middleware against a dummy app.
    calls = []

    async def dummy(scope, receive, send):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"event: endpoint\n\n"})

    mw = server_sse.BearerAuthMiddleware(dummy, TOKEN)
    assert _call_asgi(mw, "/sse", [("Authorization", f"Bearer {TOKEN}")])[0] == 200
    assert _call_asgi(mw, "/sse", [("Authorization", f"bearer {TOKEN}")])[0] == 200
    status, headers = _call_asgi(mw, "/sse")
    assert status == 401 and b"www-authenticate" in headers
    assert _call_asgi(mw, "/sse", [("Authorization", "Bearer nope")])[0] == 401
    assert calls == ["/sse", "/sse"]


def test_middleware_passes_lifespan():
    seen = []

    async def dummy(scope, receive, send):
        seen.append(scope["type"])

    mw = server_sse.BearerAuthMiddleware(dummy, TOKEN)
    asyncio.run(mw({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


# ── /health ─────────────────────────────────────────────────────────────────

def test_health_needs_no_token_and_leaks_nothing(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('FATAL: password authentication failed for user "secretuser" '
                           'host "db.internal"')

    monkeypatch.setattr(server_sse.db, "execute", boom)
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json() == {"status": "unhealthy", "database": "unavailable"}
    assert "secretuser" not in resp.text and "db.internal" not in resp.text


def test_health_ok(client, monkeypatch):
    monkeypatch.setattr(server_sse.db, "execute", lambda *a, **k: [{"?column?": 1}])
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["database"] == "connected"


# ── Startup policy ───────────────────────────────────────────────────────────

def test_env_token_is_enforced_on_any_bind(monkeypatch):
    monkeypatch.setenv("CITYDB_MCP_AUTH_TOKEN", TOKEN)
    assert server_sse.resolve_auth_token("127.0.0.1") == TOKEN
    assert server_sse.resolve_auth_token("0.0.0.0") == TOKEN


def test_loopback_without_token_runs_open(tmp_path):
    for host in ("127.0.0.1", "::1", "localhost", "[::1]"):
        assert server_sse.resolve_auth_token(host) is None
    app = server_sse.build_app(host="127.0.0.1")
    assert not isinstance(app, server_sse.BearerAuthMiddleware)
    assert not (tmp_path / "mcp_auth_token").exists()


def test_non_loopback_without_token_generates_and_enforces(tmp_path):
    token_file = tmp_path / "mcp_auth_token"
    app = server_sse.build_app(host="0.0.0.0")
    assert isinstance(app, server_sse.BearerAuthMiddleware)
    generated = token_file.read_text(encoding="utf-8").strip()
    assert len(generated) >= 32
    if os.name == "posix":
        assert (token_file.stat().st_mode & 0o777) == 0o600

    with TestClient(app, base_url=BASE) as c:
        assert c.post("/mcp/", headers=MCP_HEADERS, json=INIT).status_code == 401
        ok = c.post("/mcp/", headers={**MCP_HEADERS, **_bearer(generated)}, json=INIT)
        assert ok.status_code == 200

    # A restart reuses the stored token instead of rotating it.
    assert server_sse.resolve_auth_token("0.0.0.0") == generated


# ── DNS-rebinding protection ─────────────────────────────────────────────────

@pytest.mark.parametrize("host", ["localhost:8811", "localhost:9999", "127.0.0.1:8811",
                                  "127.0.0.1:12345", "[::1]:8811"])
def test_loopback_host_on_any_port_is_accepted(client, host):
    resp = client.post("/mcp/", headers={**MCP_HEADERS, **_bearer(), "Host": host}, json=INIT)
    assert resp.status_code == 200


def test_foreign_host_is_rejected(client):
    resp = client.post("/mcp/", headers={**MCP_HEADERS, **_bearer(), "Host": "evil.example:8811"},
                       json=INIT)
    assert resp.status_code == 421
    resp = client.get("/sse", headers={**_bearer(), "Host": "evil.example"})
    assert resp.status_code == 421


def test_foreign_origin_is_rejected(client):
    resp = client.post("/mcp/", headers={**MCP_HEADERS, **_bearer(),
                                         "Origin": "http://evil.example"}, json=INIT)
    assert resp.status_code == 403


def test_allowed_hosts_env_extends_list(monkeypatch):
    monkeypatch.setenv("CITYDB_MCP_ALLOWED_HOSTS", "mcp.example.org, other.example:8443")
    app = server_sse.build_app(token=TOKEN)
    with TestClient(app, base_url="https://mcp.example.org") as c:
        resp = c.post("/mcp/", headers={**MCP_HEADERS, **_bearer(),
                                        "Origin": "https://mcp.example.org"}, json=INIT)
        assert resp.status_code == 200
        resp = c.post("/mcp/", headers={**MCP_HEADERS, **_bearer(),
                                        "Host": "other.example:8443"}, json=INIT)
        assert resp.status_code == 200
        resp = c.post("/mcp/", headers={**MCP_HEADERS, **_bearer(),
                                        "Host": "other.example:9000"}, json=INIT)
        assert resp.status_code == 421


# ── _ctx trust ───────────────────────────────────────────────────────────────

def test_build_app_turns_ctx_trust_off():
    tool_context.trust_client_ctx = True
    server_sse.build_app(token=TOKEN)
    assert tool_context.trust_client_ctx is False
    _, ctx = tool_context.split_tool_ctx({"_ctx": {"user_id": "someone-else"}})
    assert ctx == {}
