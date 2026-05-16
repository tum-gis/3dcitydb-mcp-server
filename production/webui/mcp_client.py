"""Async MCP client that spawns citydb-mcp as a stdio subprocess.

A single background event loop is maintained for the lifetime of the process.
All sync callers submit coroutines into it via run_coroutine_threadsafe, which
is safe across concurrent Gradio sessions. The previous design used
``asyncio.run()`` per call, which created/closed a fresh loop each time and
raced under concurrent UI sessions on Python 3.10+.
"""

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is not None and not _loop.is_closed():
        return _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            t = threading.Thread(
                target=loop.run_forever,
                name="mcp-client-loop",
                daemon=True,
            )
            t.start()
            _loop = loop
        return _loop


@asynccontextmanager
async def mcp_session():
    params = StdioServerParameters(
        command="3dcitydb-mcp",
        args=[],
        env=os.environ.copy(),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call_tool(session: ClientSession, tool_name: str, arguments: dict) -> Any:
    result = await session.call_tool(tool_name, arguments)
    if result.content and hasattr(result.content[0], "text"):
        return result.content[0].text
    return result


async def assemble_system_prompt(
    include_query_agent_extras: bool = True,
    compact: bool = False,
) -> str:
    async with mcp_session() as session:
        raw = await call_tool(
            session, "assemble_prompt",
            {
                "include_query_agent_extras": include_query_agent_extras,
                "compact": compact,
            },
        )
    return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)


async def run_tool(tool_name: str, arguments: dict) -> str:
    async with mcp_session() as session:
        raw = await call_tool(session, tool_name, arguments)
    return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)


def _run_sync(coro):
    """Submit a coroutine to the persistent background loop and block."""
    loop = _get_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result()


def assemble_system_prompt_sync(
    include_query_agent_extras: bool = True,
    compact: bool = False,
) -> str:
    return _run_sync(
        assemble_system_prompt(include_query_agent_extras, compact=compact)
    )


def run_tool_sync(tool_name: str, arguments: dict) -> str:
    return _run_sync(run_tool(tool_name, arguments))
