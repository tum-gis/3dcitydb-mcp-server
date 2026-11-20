"""
3DCityDB MCP Server - HTTP transports (SSE and Streamable HTTP)

Runs the MCP server as an HTTP service. Remote clients connect over
Server-Sent Events (``/sse`` + ``/messages/``) or Streamable HTTP (``/mcp``).

Every endpoint except ``/health`` requires ``Authorization: Bearer <token>``.
The token comes from ``CITYDB_MCP_AUTH_TOKEN``. Without it, the server only
starts unauthenticated on a loopback address; on any other bind address it
generates a token, stores it in a file (``CITYDB_MCP_AUTH_TOKEN_FILE``, by
default ``/app/data/mcp_auth_token`` in Docker or the user's config
directory) and enforces it.

Usage:
    python -m citydb_mcp.server_sse --host 127.0.0.1 --port 8811
"""

import argparse
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from .server import server, db
from . import tool_context

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("citydb-mcp-sse")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8811
DEFAULT_MAX_CONNECTIONS = 32
TOKEN_FILE_NAME = "mcp_auth_token"
DOCKER_DATA_DIR = Path("/app/data")

# Paths reachable without a token (liveness probes).
PUBLIC_PATHS = frozenset({"/health"})

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")


# ── Token policy ─────────────────────────────────────────────────────────────

def is_loopback(host: str) -> bool:
    """True for 127.0.0.0/8, ::1 and localhost."""
    host = (host or "").strip().strip("[]").lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def default_token_file() -> Path:
    """Where an auto-generated token is stored.

    ``CITYDB_MCP_AUTH_TOKEN_FILE`` wins; otherwise ``/app/data`` in the Docker
    image (a bind mount to ``production/data`` on the host), else the user's
    config directory.
    """
    override = os.getenv("CITYDB_MCP_AUTH_TOKEN_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    if DOCKER_DATA_DIR.is_dir():
        return DOCKER_DATA_DIR / TOKEN_FILE_NAME
    if sys.platform == "win32" and os.getenv("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "3dcitydb-mcp" / TOKEN_FILE_NAME


def _load_or_create_token(path: Path) -> str:
    """Reuse the token stored in ``path``, or create one (file mode 0600)."""
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def resolve_auth_token(host: str) -> str | None:
    """Apply the fail-closed startup policy.

    - ``CITYDB_MCP_AUTH_TOKEN`` set: enforce it on any bind address.
    - unset, loopback bind: no authentication (warning logged).
    - unset, any other bind: load or generate a token file and enforce it.
    """
    token = os.getenv("CITYDB_MCP_AUTH_TOKEN", "").strip()
    if token:
        return token
    if is_loopback(host):
        logger.warning(
            "CITYDB_MCP_AUTH_TOKEN is not set: the HTTP endpoints accept "
            "unauthenticated requests from this machine (bound to %s). Any "
            "local process or user can call every tool.", host)
        return None
    path = default_token_file()
    token = _load_or_create_token(path)
    logger.warning(
        "CITYDB_MCP_AUTH_TOKEN is not set and the server binds to %s; a "
        "bearer token is required. It is stored in %s (send it as "
        "'Authorization: Bearer <token>').", host, path)
    return token


# ── DNS-rebinding protection ─────────────────────────────────────────────────

def _extra_allowed_hosts() -> list[str]:
    raw = os.getenv("CITYDB_MCP_ALLOWED_HOSTS", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def build_security_settings(extra_hosts: list[str] | None = None):
    """TransportSecuritySettings for the SSE and Streamable HTTP transports.

    Loopback names are accepted on any port, so a remapped Docker host port
    keeps working. ``CITYDB_MCP_ALLOWED_HOSTS`` (comma-separated ``host`` or
    ``host:port``; ``host:*`` for any port) adds names, e.g. the public name
    behind a reverse proxy. A single ``*`` turns the Host/Origin check off
    (the bearer token is still required).
    """
    from mcp.server.transport_security import TransportSecuritySettings

    extra = _extra_allowed_hosts() if extra_hosts is None else list(extra_hosts)
    if "*" in extra:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    hosts: list[str] = []
    origins: list[str] = []
    for name in list(_LOOPBACK_HOSTS) + extra:
        hosts.append(name)
        for scheme in ("http", "https"):
            origins.append(f"{scheme}://{name}")
        if ":" not in name.replace("[::1]", ""):
            hosts.append(f"{name}:*")
            for scheme in ("http", "https"):
                origins.append(f"{scheme}://{name}:*")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


# ── Bearer-token middleware ──────────────────────────────────────────────────

class BearerAuthMiddleware:
    """Pure ASGI middleware requiring ``Authorization: Bearer <token>``.

    Starlette's BaseHTTPMiddleware is deliberately not used: it buffers
    responses and breaks SSE streaming.
    """

    def __init__(self, app, token: str, public_paths=PUBLIC_PATHS):
        self.app = app
        self._token = token.encode("utf-8")
        self._public = frozenset(public_paths)

    def _authorized(self, scope) -> bool:
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                scheme, _, credential = value.partition(b" ")
                if scheme.lower() == b"bearer":
                    return hmac.compare_digest(credential.strip(), self._token)
                return False
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope.get("path") in self._public or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"error": "unauthorized"}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", b'Bearer realm="3dcitydb-mcp"'),
            ],
        })
        await send({"type": "http.response.body", "body": body})


# ── App factory ──────────────────────────────────────────────────────────────

async def _response_already_sent(scope, receive, send):
    """No-op ASGI response for endpoints that wrote to ``send`` themselves."""
    return None


def build_app(token: str | None = None, host: str = DEFAULT_HOST,
              allowed_hosts: list[str] | None = None):
    """Build the ASGI app.

    ``token=None`` applies the startup policy in :func:`resolve_auth_token`;
    a string enforces that token. ``allowed_hosts`` extends the DNS-rebinding
    allow-list (default: ``CITYDB_MCP_ALLOWED_HOSTS``).
    """
    from mcp.server.sse import SseServerTransport
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount
    from starlette.responses import JSONResponse

    # HTTP clients cannot be trusted to name the user: ignore their _ctx.
    tool_context.trust_client_ctx = False

    if token is None:
        token = resolve_auth_token(host)

    security = build_security_settings(allowed_hosts)

    # SSE transport
    sse = SseServerTransport("/messages/", security_settings=security)

    # Streamable HTTP transport (preferred by modern clients, e.g. VS Code,
    # which POSTs to the configured URL and only falls back to SSE on 404).
    http = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        json_response=True,
        security_settings=security,
    )

    @asynccontextmanager
    async def lifespan(_: Starlette):
        async with http.run():
            yield
        try:
            db.close()
        except Exception:
            pass

    async def handle_sse(request):
        try:
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await server.run(
                    streams[0], streams[1], server.create_initialization_options()
                )
        except Exception as exc:
            # Client disconnect / stream error: log and return; do not let the
            # exception bubble up and tear down the worker.
            logger.warning("SSE session ended with error: %s", exc)
        # The transport has already sent the response (stream or error).
        return _response_already_sent

    async def health_check(request):
        """Health check endpoint (no token required, no error details)."""
        try:
            db.execute("SELECT 1")
            return JSONResponse({"status": "healthy", "database": "connected"})
        except Exception as exc:
            # Details stay in the server log; the response is generic.
            logger.warning("Health check: database unavailable: %s", exc)
            return JSONResponse(
                {"status": "unhealthy", "database": "unavailable"},
                status_code=503,
            )

    app = Starlette(
        debug=False,
        routes=[
            Route("/health", health_check),
            Route("/sse", endpoint=handle_sse),
            Mount("/mcp", http.handle_request),
            Mount("/messages/", app=sse.handle_post_message),
        ],
        lifespan=lifespan,
    )

    if token:
        return BearerAuthMiddleware(app, token)
    return app


def _max_connections() -> int:
    try:
        return max(1, int(os.getenv("CITYDB_MCP_MAX_CONNECTIONS", "")))
    except ValueError:
        return DEFAULT_MAX_CONNECTIONS


def main():
    parser = argparse.ArgumentParser(
        description="3DCityDB MCP Server (HTTP: SSE + Streamable HTTP)")
    parser.add_argument(
        "--host", default=os.getenv("MCP_SSE_HOST") or DEFAULT_HOST,
        help=f"Host to bind to (default: {DEFAULT_HOST}; env MCP_SSE_HOST)")
    parser.add_argument(
        "--port", type=int,
        default=int(os.getenv("MCP_SSE_PORT") or DEFAULT_PORT),
        help=f"Port to listen on (default: {DEFAULT_PORT}; env MCP_SSE_PORT)")
    args = parser.parse_args()

    import uvicorn

    app = build_app(host=args.host)
    auth = "bearer token required" if isinstance(app, BearerAuthMiddleware) else "NONE"

    logger.info(f"Starting 3DCityDB MCP Server (HTTP) on {args.host}:{args.port}")
    logger.info(f"Authentication: {auth}")
    logger.info(f"SSE endpoint: http://{args.host}:{args.port}/sse")
    logger.info(f"Streamable HTTP endpoint: http://{args.host}:{args.port}/mcp")
    logger.info(f"Health check: http://{args.host}:{args.port}/health")

    uvicorn.run(app, host=args.host, port=args.port,
                limit_concurrency=_max_connections())


if __name__ == "__main__":
    main()
