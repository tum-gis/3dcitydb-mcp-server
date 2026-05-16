"""
CityGML Context MCP Server - SSE Transport

Runs the MCP server as an HTTP service using Server-Sent Events (SSE).
This allows remote clients to connect over HTTP.

Usage:
    python -m citygml_mcp.server_sse --host 0.0.0.0 --port 8080
"""

import argparse
import logging
from .server import server, db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("citygml-mcp-sse")


def main():
    parser = argparse.ArgumentParser(description="CityGML MCP Server (SSE)")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    args = parser.parse_args()

    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount
    from starlette.responses import JSONResponse
    import uvicorn

    # SSE transport
    sse = SseServerTransport("/messages/")

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

    async def health_check(request):
        """Health check endpoint."""
        try:
            db.execute("SELECT 1")
            return JSONResponse({"status": "healthy", "database": "connected"})
        except Exception as e:
            return JSONResponse(
                {"status": "unhealthy", "database": str(e)},
                status_code=503
            )

    async def _on_shutdown():
        try:
            db.close()
        except Exception:
            pass

    app = Starlette(
        debug=False,
        routes=[
            Route("/health", health_check),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
        on_shutdown=[_on_shutdown],
    )

    logger.info(f"Starting CityGML MCP Server (SSE) on {args.host}:{args.port}")
    logger.info(f"SSE endpoint: http://{args.host}:{args.port}/sse")
    logger.info(f"Health check: http://{args.host}:{args.port}/health")

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
